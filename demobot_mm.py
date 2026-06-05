#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
demobot_mm.py — Mattermost-Adapter, Dialog-Modell (Phase 1).

Ablauf:
- DIALOG (Standard): jede Nachricht -> AI ANTWORTET, bespricht, schaut Dateien an,
  aendert aber NICHTS (Planungs-Gespraech). "Mit der AI entwickeln."
- COMMIT ("go" / "deckel drauf"): AI SETZT die besprochene Aufgabe um (volle Tools, live).
- Live-Sicht: jeder Schritt wird in EINE Status-Nachricht gestreamt.
- STEUERN: "was laeuft" / "stop #N".
- System-Meldungen (join/leave/add) werden ignoriert.
- Owner-only. Datei-Uploads -> _inbox\\, Ergebnis-Dateien aus _outbox\\ -> Chat.
"""

import os
import sys
import json
import time
import re
import logging
import threading

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
from dotenv import load_dotenv
from mattermostdriver import Driver

load_dotenv()

import demobot_core as core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("demobot-mm")

MM_URL = os.environ["MM_URL"]
MM_TOKEN = os.environ["MM_TOKEN"]
MM_CHANNEL_ID = os.environ["MM_CHANNEL_ID_DEMOBOT"]
MM_SCHEME = os.environ.get("MM_SCHEME", "https")
MM_PORT = int(os.environ.get("MM_PORT", "443"))
MM_OWNER = os.environ.get("MM_OWNER_USER_ID", "")
CHANNEL_NAME = os.environ.get("DEMOBOT_CHANNEL_NAME", "demobot")
MAX_PARALLEL = int(os.environ.get("DEMOBOT_MAX_PARALLEL", "5"))

API_BASE = f"{MM_SCHEME}://{MM_URL}/api/v4"
AUTH_H = {"Authorization": "Bearer " + MM_TOKEN}
INBOX = os.path.join(core.dir_for(CHANNEL_NAME), "_inbox")

driver = None
BOT_USER_ID = None
_sem = threading.Semaphore(MAX_PARALLEL)
_tasks = {}
_task_lock = threading.Lock()
_task_seq = [0]

GO_WORDS = ["deckel drauf", "deckeldrauf", "leg los", "mach es", "ausführen",
            "ausfuehren", "umsetzen", "go", "los", "mach", "jetzt", "run"]
STATUS_WORDS = {"was läuft", "was laeuft", "was läuft?", "was laeuft?", "status",
                "wer arbeitet", "was machst du"}

PLAN_HINT = (
    "MODUS: GEMEINSAM ENTWICKELN (Dialog/Planung). Besprich die Aufgabe mit dem User, "
    "frag nach, mach Vorschlaege, schau bei Bedarf Dateien an (nur LESEN). "
    "AENDERE/ERSTELLE/LOESCHE NICHTS, fuehre keine Scripts/DB-Schreibungen aus, "
    "solange der User nicht 'go' bzw. 'Deckel drauf' gesagt hat. Antworte kurz im Dialog."
)
EXEC_HINT = (
    "MODUS: JETZT UMSETZEN ('Deckel drauf'). Setze die zuvor besprochene Aufgabe "
    "vollstaendig um — du darfst jetzt alles (Dateien, SQLite, Scripts). "
    "Ergebnis-Dateien, die der User erhalten soll, nach _outbox/ legen."
)


def _make_driver():
    return Driver({"url": MM_URL, "token": MM_TOKEN, "scheme": MM_SCHEME,
                   "port": MM_PORT, "verify": True, "timeout": 30})


def _post_text(text):
    text = text or "(leer)"
    for i in range(0, len(text), 16000):
        driver.posts.create_post({"channel_id": MM_CHANNEL_ID, "message": text[i:i + 16000]})


def _create_post(msg):
    return driver.posts.create_post({"channel_id": MM_CHANNEL_ID, "message": msg})


def _patch(post_id, msg):
    try:
        driver.posts.patch_post(post_id, {"id": post_id, "message": msg[:16000]})
    except Exception:
        pass


def _safe_name(name):
    keep = "-_.() abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(c for c in (name or "datei") if c in keep).strip() or "datei"


def _download_incoming(post):
    pfade = []
    files_meta = (post.get("metadata") or {}).get("files") or []
    if not files_meta:
        return pfade
    os.makedirs(INBOX, exist_ok=True)
    for fm in files_meta:
        fid = fm.get("id")
        name = _safe_name(fm.get("name") or fid)
        try:
            r = requests.get(f"{API_BASE}/files/{fid}", headers=AUTH_H, timeout=120)
            r.raise_for_status()
        except Exception:
            log.warning("Datei nicht ladbar: %s", fid)
            continue
        ziel = os.path.join(INBOX, name)
        base, ext = os.path.splitext(ziel)
        i = 1
        while os.path.exists(ziel):
            ziel = f"{base}_{i}{ext}"
            i += 1
        with open(ziel, "wb") as f:
            f.write(r.content)
        pfade.append(ziel)
    return pfade


def _post_file(path):
    with open(path, "rb") as f:
        r = requests.post(f"{API_BASE}/files", headers=AUTH_H,
                          data={"channel_id": MM_CHANNEL_ID},
                          files={"files": (os.path.basename(path), f)}, timeout=180)
    r.raise_for_status()
    fid = r.json()["file_infos"][0]["id"]
    driver.posts.create_post({"channel_id": MM_CHANNEL_ID, "message": "", "file_ids": [fid]})


def _next_tid():
    with _task_lock:
        _task_seq[0] += 1
        return _task_seq[0]


def _render_live(tk):
    icon = {"läuft": "▶️", "fertig": "✅", "abgebrochen": "⏹️", "fehler": "❌"}.get(tk["status"], "▶️")
    head = f"{icon} **#{tk['id']}** {tk['status']} — _{tk['title']}_"
    steps = tk["steps"][-8:]
    return head + ("\n" + "\n".join(steps) if steps else "")


def _update_live(tk, force=False):
    now = time.time()
    if not force and now - tk.get("last", 0) < 1.3:
        return
    tk["last"] = now
    _patch(tk["post_id"], _render_live(tk))


def _list_tasks():
    with _task_lock:
        if not _tasks:
            return "Keine Aufgaben bisher."
        recent = sorted(_tasks.values(), key=lambda x: x["id"], reverse=True)[:6]
    icons = {"läuft": "▶️", "fertig": "✅", "abgebrochen": "⏹️", "fehler": "❌"}
    lines = ["**Aufgaben:**"]
    for tk in recent:
        last = tk["steps"][-1] if tk["steps"] else ""
        suffix = f" — {last}" if tk["status"] == "läuft" and last else ""
        lines.append(f"{icons.get(tk['status'], '·')} #{tk['id']} [{tk['status']}] {tk['title']}{suffix}")
    return "\n".join(lines)


def _stop(tid):
    with _task_lock:
        tk = _tasks.get(tid)
    if not tk:
        return f"Keine Aufgabe #{tid}."
    if tk["status"] != "läuft":
        return f"#{tid} läuft nicht (Status: {tk['status']})."
    tk["status"] = "abgebrochen"
    core.kill_proc(tk.get("proc"))
    _update_live(tk, force=True)
    return f"⏹️ #{tid} wird abgebrochen."


def _process(text, files):
    tid = _next_tid()
    title = (text.splitlines()[0][:55] if text else ("Datei-Aufgabe" if files else "Aufgabe"))
    post = _create_post(f"▶️ **#{tid}** läuft … _{title}_")
    tk = {"id": tid, "title": title, "status": "läuft", "proc": None,
          "post_id": post["id"], "steps": [], "last": 0.0}
    with _task_lock:
        _tasks[tid] = tk

    def on_start(proc):
        tk["proc"] = proc

    def on_progress(step):
        if tk["status"] == "abgebrochen":
            return
        tk["steps"].append(step)
        _update_live(tk)

    with _sem:
        if tk["status"] == "abgebrochen":
            return
        try:
            reply, outfiles = core.run_stream(CHANNEL_NAME, text, files,
                                              on_progress=on_progress, on_start=on_start)
            if tk["status"] == "abgebrochen":
                _patch(tk["post_id"], f"⏹️ **#{tid}** abgebrochen — _{title}_")
                return
            tk["status"] = "fertig"
            _patch(tk["post_id"], f"✅ **#{tid}** — _{title}_\n\n{(reply or '')[:15000]}")
            sent = []
            for p in outfiles:
                try:
                    _post_file(p)
                    sent.append(p)
                    log.info("Datei gesendet: %s", os.path.basename(p))
                except Exception:
                    log.exception("Konnte Datei nicht senden: %s", p)
            if sent:
                core.archive_sent(CHANNEL_NAME, sent)
        except Exception as e:
            log.exception("Verarbeitung fehlgeschlagen")
            tk["status"] = "fehler"
            _patch(tk["post_id"], f"❌ **#{tid}** Fehler — {e}")
        finally:
            tk["proc"] = None


def _handle_post(post, sender_name):
    user_id = post.get("user_id", "")
    if MM_OWNER and user_id != MM_OWNER:
        return
    text = (post.get("message") or "").strip()
    incoming = _download_incoming(post)
    low = text.lower().strip()
    low_c = low.rstrip("!?. ")

    # Status
    if low_c in STATUS_WORDS or low in STATUS_WORDS:
        _post_text(_list_tasks())
        return
    # Stop
    m = re.match(r"(?:stop|unterbrich|abbrechen|abbruch|halt)\s*#?(\d+)", low)
    if m:
        _post_text(_stop(int(m.group(1))))
        return
    # Sofort ausfuehren — der demobot macht es direkt
    if not text and not incoming:
        return
    log.info("[demobot] %s: %s %s", sender_name, text,
             f"[+{len(incoming)} Datei]" if incoming else "")
    _process(text, incoming)


def _run_task(post, sender):
    try:
        _handle_post(post, sender)
    except Exception:
        log.exception("Fehler bei der Verarbeitung")


async def event_handler(message):
    try:
        data = json.loads(message)
    except Exception:
        return
    if data.get("event") != "posted":
        return
    try:
        post = json.loads(data["data"]["post"])
    except Exception:
        return
    if post.get("channel_id") != MM_CHANNEL_ID:
        return
    if post.get("user_id") == BOT_USER_ID:
        return
    # System-Meldungen (join/leave/add/header…) ignorieren — kein Input
    if (post.get("type") or "").startswith("system_"):
        return
    sender_name = (data["data"].get("sender_name") or "").lstrip("@") or "user"
    threading.Thread(target=_run_task, args=(post, sender_name), daemon=True).start()


def main():
    global driver, BOT_USER_ID
    os.makedirs(INBOX, exist_ok=True)
    while True:
        try:
            driver = _make_driver()
            driver.login()
            me = driver.users.get_user("me")
            BOT_USER_ID = me["id"]
            log.info("Verbunden als @%s | Kanal %s -> %s | Maschine %s | Owner-only:%s",
                     me.get("username"), CHANNEL_NAME, core.dir_for(CHANNEL_NAME),
                     core.MACHINE, bool(MM_OWNER))
            driver.init_websocket(event_handler)
            log.warning("WebSocket beendet — reconnect in 5s")
        except Exception:
            log.exception("Verbindungsfehler — reconnect in 5s")
        time.sleep(5)


if __name__ == "__main__":
    main()
