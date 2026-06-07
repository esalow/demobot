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
import datetime
from datetime import timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
import ssl as _ssl
from dotenv import load_dotenv
from mattermostdriver import Driver
import mattermostdriver.websocket as _mmws

# Fix fuer Python 3.13: mattermostdriver 7.3.2 baut den Client-WebSocket-SSL-Kontext
# faelschlich mit ssl.Purpose.CLIENT_AUTH (= Server-Kontext). Python <=3.12 schluckt das,
# 3.13 lehnt es ab ("Cannot create a client socket with a PROTOCOL_TLS_SERVER context").
# Wir ersetzen den ssl-Namen NUR in diesem Modul durch einen Shim, der den korrekten
# Client-Kontext (SERVER_AUTH) erzwingt. Auf allen Python-Versionen unschaedlich.
class _SSLClientShim:
    Purpose = _ssl.Purpose
    CERT_NONE = _ssl.CERT_NONE

    def create_default_context(self, *a, **k):
        k["purpose"] = _ssl.Purpose.SERVER_AUTH
        return _ssl.create_default_context(*a, **k)

    def __getattr__(self, name):
        return getattr(_ssl, name)


_mmws.ssl = _SSLClientShim()

load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"), override=True)

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
AUFGABEN_MAX = int(os.environ.get("DEMOBOT_MAX_AUFGABEN", "2"))
SESSION_TTL = int(os.environ.get("DEMOBOT_SESSION_TTL", str(2 * 3600)))  # Default 2h

API_BASE = f"{MM_SCHEME}://{MM_URL}/api/v4"
AUTH_H = {"Authorization": "Bearer " + MM_TOKEN}
DEMOBOT_DIR = core.dir_for(CHANNEL_NAME)
INBOX = os.path.join(DEMOBOT_DIR, "_inbox")
OUTBOX = os.path.join(DEMOBOT_DIR, "_outbox")
DIALOG_LOG = os.path.join(DEMOBOT_DIR, "logs", "dialog.jsonl")

# --- Aktives Projekt/Vorgang-State ---
_STATE_FILE = os.path.join(DEMOBOT_DIR, "_bot_state.json")
_state_lock = threading.Lock()
_active_state = {"name": CHANNEL_NAME, "type": "kanal", "dir": DEMOBOT_DIR}


def _load_state():
    global _active_state
    with _state_lock:
        if os.path.exists(_STATE_FILE):
            try:
                data = json.load(open(_STATE_FILE, encoding="utf-8"))
                _active_state.update(data)
                # Task-Counter wiederherstellen
                _task_seq[0] = data.get("task_seq", 0)
            except Exception:
                pass


def _save_state():
    _active_state["task_seq"] = _task_seq[0]
    with open(_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(_active_state, fh, ensure_ascii=False, indent=2)


def _touch_activity():
    with _state_lock:
        _active_state["last_activity"] = datetime.datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save_state()


def _check_and_reset_ttl():
    """Prüft TTL. Wenn abgelaufen: Hook aufrufen, Session löschen, Meldung posten. Gibt True zurück wenn reset."""
    with _state_lock:
        last = _active_state.get("last_activity")
    if not last:
        return False
    try:
        last_dt = datetime.datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        delta = (datetime.datetime.now(timezone.utc) - last_dt).total_seconds()
    except Exception:
        return False
    if delta <= SESSION_TTL:
        return False

    # TTL überschritten — Hook aufrufen vor Session-Löschen
    sid = core._load_session(core.dir_for(CHANNEL_NAME), CHANNEL_NAME)
    if sid:
        _run_precompact_hook(sid)

    # Session löschen
    core._save_session(core.dir_for(CHANNEL_NAME), CHANNEL_NAME, None)
    log.info("Session-TTL überschritten (%.0f min) — Session resettet", delta / 60)
    _post_text(f"\U0001f504 Neue Session gestartet — letzte Aktivität vor {int(delta / 3600)}h {int((delta % 3600) / 60)}min.")
    return True


def _run_precompact_hook(session_id):
    """Ruft den PreCompact-Hook manuell auf (vor TTL-Reset)."""
    hook = r"C:\Users\Lenovo T460p\.claude\scripts\precompact-hook.py"
    if not os.path.exists(hook):
        return
    import subprocess as _sp
    hook_input = json.dumps({"session_id": session_id, "trigger": "ttl_reset"})
    try:
        _sp.run(
            [r"C:\Users\Lenovo T460p\AppData\Local\Programs\Python\Python313\python.exe", hook],
            input=hook_input, capture_output=True, text=True, timeout=20
        )
        log.info("PreCompact-Hook aufgerufen für Session %s", session_id)
    except Exception as e:
        log.warning("PreCompact-Hook fehlgeschlagen: %s", e)


def _get_active():
    with _state_lock:
        return dict(_active_state)


def _set_active(name, typ, directory):
    with _state_lock:
        _active_state["name"] = name
        _active_state["type"] = typ
        _active_state["dir"] = directory
        _save_state()


def _create_projekt(name, d):
    os.makedirs(os.path.join(d, "_inbox"), exist_ok=True)
    os.makedirs(os.path.join(d, "_outbox"), exist_ok=True)
    claude_md = os.path.join(d, "CLAUDE.md")
    if not os.path.exists(claude_md):
        with open(claude_md, "w", encoding="utf-8") as fh:
            fh.write(f"# {name}\n\nVia Mattermost-Demobot angelegt am "
                     f"{datetime.date.today().isoformat()}.\n")
    _register_in_registry(name, typ="projekt", directory=d)


def _create_vorgang(name, d):
    os.makedirs(d, exist_ok=True)
    vorgang_md = os.path.join(d, "VORGANG.md")
    if not os.path.exists(vorgang_md):
        with open(vorgang_md, "w", encoding="utf-8") as fh:
            fh.write(f"# Vorgang: {name}\n\nAngelegt via Mattermost-Demobot am "
                     f"{datetime.date.today().isoformat()}.\n\n## Einträge\n\n")
    status_json = os.path.join(d, "status.json")
    if not os.path.exists(status_json):
        with open(status_json, "w", encoding="utf-8") as fh:
            json.dump({"name": name, "status": "offen",
                       "erstellt": datetime.date.today().isoformat(),
                       "working_path": d}, fh, ensure_ascii=False, indent=2)
    _register_in_registry(name, typ="vorgang", directory=d)


def _register_in_registry(name, typ, directory):
    registry = os.path.join(core.CLAUDE_META, "project_registry.md")
    if not os.path.exists(registry):
        return
    try:
        content = open(registry, encoding="utf-8").read()
        if name in content:
            return
        line = f"| {name} | {typ} | {directory} | remote angelegt {datetime.date.today().isoformat()} |\n"
        with open(registry, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def _log_dialog(sender, user_text, reply, task_id, active_dir=None,
                files_in=None, files_out=None, status="fertig", aufgabe_id=None):
    os.makedirs(os.path.dirname(DIALOG_LOG), exist_ok=True)
    active_dir = active_dir or DEMOBOT_DIR
    active = _get_active()
    entry = {
        "ts": datetime.datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_id": task_id,
        "aufgabe_id": f"A{aufgabe_id}" if aufgabe_id else None,
        "sender": sender,
        "projekt": active["name"],
        "typ": active["type"],
        "in": user_text or "",
        "files_in": [os.path.basename(p) for p in (files_in or [])],
        "files_source": INBOX,
        "out": reply or "",
        "files_out": [os.path.basename(p) for p in (files_out or [])],
        "status": status,
    }
    with open(DIALOG_LOG, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
    # Doppelt schreiben: _remote_log.jsonl im Zielprojekt (nur wenn nicht demobot selbst)
    if active_dir != DEMOBOT_DIR:
        remote_log = os.path.join(active_dir, "_remote_log.jsonl")
        try:
            with open(remote_log, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except Exception:
            pass

driver = None
BOT_USER_ID = None
_sem = threading.Semaphore(AUFGABEN_MAX)
_tasks = {}
_task_lock = threading.Lock()
_task_seq = [0]

# --- Multi-Aufgaben-State ---
_aufgaben = {}          # int -> {id, title, session_key, status}
_aufgabe_seq = [0]
_cur_aufgabe = [None]   # aktuell aktive Aufgabe-ID (None = Einzel-Modus)
_await_select = [False] # True = warte auf A1/A2/... nach "zurueck"
_aufgaben_lock = threading.Lock()

NEUE_RE = re.compile(
    r"\b(andere|neue|n[aä]chste|zweite|dritte|noch.?eine|au[sß]erdem)\b.{0,20}"
    r"\b(aufgabe|aufgab|sache|task)\b", re.I)
REF_RE = re.compile(r"^[Aa#](\d+)\s*(.*)", re.S)
# Erkennt "zu A1", "Aufgabe A1", "zurück zu a 1", "zu aufgabe 1" irgendwo im Text
REF_ANYWHERE_RE = re.compile(
    r"\b(?:zu\s+)?(?:aufgabe\s+)?[Aa]\.?\s*(\d+)\b", re.I)

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
        tid = _task_seq[0]
    _save_state()
    return tid


def _render_live(tk):
    icon = {"läuft": "▶️", "fertig": "✅", "abgebrochen": "⏹️", "fehler": "❌"}.get(tk["status"], "▶️")
    a_label = f" [A{tk['aufgabe_id']}]" if tk.get("aufgabe_id") else ""
    head = f"{icon} **#{tk['id']}**{a_label} {tk['status']} — _{tk['title']}_"
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
        a_label = f" [A{tk['aufgabe_id']}]" if tk.get("aufgabe_id") else ""
        lines.append(f"{icons.get(tk['status'], '·')} #{tk['id']}{a_label} [{tk['status']}] {tk['title']}{suffix}")
    return "\n".join(lines)


def _open_aufgabe(title):
    with _aufgaben_lock:
        _aufgabe_seq[0] += 1
        aid = _aufgabe_seq[0]
        _aufgaben[aid] = {"id": aid, "title": title[:50],
                          "session_key": f"{CHANNEL_NAME}_A{aid}", "status": "aktiv"}
        _cur_aufgabe[0] = aid
    return aid


def _aufgaben_liste():
    with _aufgaben_lock:
        if not _aufgaben:
            return "Keine aktiven Aufgaben."
        lines = ["**Aufgaben:**"]
        for aid, a in sorted(_aufgaben.items()):
            icon = "▶️" if a["status"] == "aktiv" else "✅"
            lines.append(f"{icon} **A{aid}** — {a['title']}")
        lines.append("\n➡️ Antworte mit **A1**, **A2**, ... um fortzusetzen.")
    return "\n".join(lines)


def _get_session_key(aufgabe_id):
    if aufgabe_id is None:
        return CHANNEL_NAME
    with _aufgaben_lock:
        a = _aufgaben.get(aufgabe_id)
    return a["session_key"] if a else CHANNEL_NAME


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


def _process(text, files, sender="user", aufgabe_id=None):
    active = _get_active()
    work_dir = active["dir"]
    tid = _next_tid()
    session_key = _get_session_key(aufgabe_id)
    proj_label = "" if active["name"] == CHANNEL_NAME else f" [{active['name']}]"
    aufgabe_label = f" [A{aufgabe_id}]" if aufgabe_id else ""
    title = (text.splitlines()[0][:55] if text else ("Datei-Aufgabe" if files else "Aufgabe"))
    post = _create_post(f"▶️ **#{tid}**{proj_label}{aufgabe_label} läuft … _{title}_")
    tk = {"id": tid, "title": title, "status": "läuft", "proc": None,
          "post_id": post["id"], "steps": [], "last": 0.0,
          "aufgabe_id": aufgabe_id}
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
            reply, outfiles = core.run_stream(
                session_key, text, files,
                on_progress=on_progress, on_start=on_start,
                work_dir=work_dir, inbox_dir=INBOX, outbox_dir=OUTBOX)
            if tk["status"] == "abgebrochen":
                _patch(tk["post_id"], f"⏹️ **#{tid}** abgebrochen — _{title}_")
                _log_dialog(sender, text, None, tid, work_dir, files, [], "abgebrochen", aufgabe_id)
                return
            tk["status"] = "fertig"
            # SWITCH-Signal auswerten (erste Zeile der Antwort)
            reply_lines = (reply or "").splitlines()
            if reply_lines and reply_lines[0].startswith("SWITCH:"):
                parts = reply_lines[0].strip().split(":")
                if len(parts) == 3:
                    sw_typ, sw_name = parts[1], parts[2].strip()
                    if sw_typ == "projekt":
                        _post_text(_cmd_projekt(sw_name))
                    elif sw_typ == "vorgang":
                        _post_text(_cmd_vorgang(sw_name))
                reply = "\n".join(reply_lines[1:]).strip()
            _patch(tk["post_id"], f"✅ **#{tid}**{proj_label}{aufgabe_label} — _{title}_\n\n{(reply or '')[:15000]}")
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
            _log_dialog(sender, text, reply, tid, work_dir, files, sent, "fertig", aufgabe_id)
            # Aufgabe als fertig markieren
            if aufgabe_id:
                with _aufgaben_lock:
                    if aufgabe_id in _aufgaben:
                        _aufgaben[aufgabe_id]["status"] = "fertig"
        except Exception as e:
            log.exception("Verarbeitung fehlgeschlagen")
            tk["status"] = "fehler"
            _patch(tk["post_id"], f"❌ **#{tid}** Fehler — {e}")
            _log_dialog(sender, text, str(e), tid, work_dir, files, [], "fehler", aufgabe_id)
        finally:
            tk["proc"] = None


def _cmd_projekt(name):
    if not name:
        a = _get_active()
        return f"Aktiv: **{a['name']}** ({a['type']}) → `{a['dir']}`"
    name = name.strip().replace(" ", "_")
    d = os.path.join(core.BASE_DIR, name)
    exists = os.path.isdir(d)
    if not exists:
        _create_projekt(name, d)
        msg = f"📁 Projekt **{name}** angelegt und aktiviert → `{d}`"
    else:
        msg = f"📂 Projekt **{name}** aktiviert → `{d}`"
    _set_active(name, "projekt", d)
    return msg


def _cmd_vorgang(name):
    if not name:
        a = _get_active()
        return f"Aktiv: **{a['name']}** ({a['type']}) → `{a['dir']}`"
    d = os.path.join(core.VORGANG_BASE, name.upper())
    exists = os.path.isdir(d)
    if not exists:
        _create_vorgang(name.upper(), d)
        msg = f"📋 Vorgang **{name.upper()}** angelegt und aktiviert → `{d}`"
    else:
        msg = f"📋 Vorgang **{name.upper()}** aktiviert → `{d}`"
    _set_active(name.upper(), "vorgang", d)
    return msg


def _cmd_zurueck():
    _set_active(CHANNEL_NAME, "kanal", DEMOBOT_DIR)
    return f"↩️ Zurück zu **{CHANNEL_NAME}** (Kanal-Standard)"


def _handle_post(post, sender_name, aufgabe_id=None):
    user_id = post.get("user_id", "")
    if MM_OWNER and user_id != MM_OWNER:
        return
    text = (post.get("message") or "").strip()
    incoming = _download_incoming(post)
    low = text.lower().strip()
    low_c = low.rstrip("!?. ")

    # Projekt wechseln — NUR mit führendem /  (ohne Slash = normaler Chat)
    m = re.match(r"^/projekt\s+(.+)", low_c)
    if m:
        _post_text(_cmd_projekt(m.group(1).strip()))
        return
    if low_c in {"/projekt", "/project"}:
        _post_text(_cmd_projekt(""))
        return

    # Vorgang wechseln — NUR mit /
    m = re.match(r"^/vorgang\s+(.+)", low_c)
    if m:
        _post_text(_cmd_vorgang(m.group(1).strip()))
        return
    if low_c in {"/vorgang"}:
        _post_text(_cmd_vorgang(""))
        return

    # Zurück zu demobot
    if low_c in {"/zurück", "/zurueck", "/home"}:
        _post_text(_cmd_zurueck())
        return

    # Status
    if low_c in STATUS_WORDS or low in STATUS_WORDS:
        _post_text(_list_tasks())
        return

    # Stop
    m = re.match(r"(?:stop|unterbrich|abbrechen|abbruch|halt)\s*#?(\d+)", low)
    if m:
        _post_text(_stop(int(m.group(1))))
        return

    if not text and not incoming:
        return

    _check_and_reset_ttl()
    _touch_activity()
    active = _get_active()
    log.info("[demobot:%s] %s: %s %s", active["name"], sender_name, text,
             f"[+{len(incoming)} Datei]" if incoming else "")
    _process(text, incoming, sender=sender_name, aufgabe_id=aufgabe_id)


def _run_task(post, sender, aufgabe_id=None):
    try:
        _handle_post(post, sender, aufgabe_id=aufgabe_id)
    except Exception:
        log.exception("Fehler bei der Verarbeitung")


# Debounce: Nachrichten pro Sender sammeln, 3s warten, dann zusammenführen.
# Verhindert dass Spracheingabe-Fragmente einzeln verarbeitet werden.
DEBOUNCE_SECONDS = float(os.environ.get("DEMOBOT_DEBOUNCE", "3.0"))
_debounce_lock = threading.Lock()
_debounce_buffers = {}   # sender_id -> {"timer": Timer, "posts": [...], "sender_name": str, "files": [...]}


def _flush_debounce(user_id):
    with _debounce_lock:
        buf = _debounce_buffers.pop(user_id, None)
    if not buf:
        return
    posts = buf["posts"]
    sender_name = buf["sender_name"]
    texts = [p.get("message", "").strip() for p in posts if p.get("message", "").strip()]
    merged_text = "\n".join(texts)
    all_file_ids = []
    for p in posts:
        for fid in (p.get("file_ids") or []):
            if fid not in all_file_ids:
                all_file_ids.append(fid)
    merged_post = dict(posts[-1])
    merged_post["message"] = merged_text
    merged_post["file_ids"] = all_file_ids
    if len(posts) > 1:
        log.info("[debounce] %d Nachrichten zusammengefuehrt: %r", len(posts), merged_text[:120])

    low = merged_text.lower().strip()

    # "zurueck" / "was laeuft" / "aufgaben" → Liste zeigen, auf Auswahl warten
    if re.match(r"^(zur[uü]ck|zurueck|back|was l[aä]uft|aufgaben|welche aufgabe)", low):
        with _aufgaben_lock:
            hat_aufgaben = bool(_aufgaben)
        if hat_aufgaben:
            _post_text(_aufgaben_liste())
            _await_select[0] = True
            return
        # keine Aufgaben → normal verarbeiten
        threading.Thread(target=_run_task, args=(merged_post, sender_name), daemon=True).start()
        return

    # Auswahl-Modus nach "zurueck": "1" oder "A2" → zu Aufgabe wechseln
    if _await_select[0]:
        m = re.match(r"^[Aa#]?(\d+)$", low)
        if m:
            aid = int(m.group(1))
            with _aufgaben_lock:
                if aid in _aufgaben:
                    _cur_aufgabe[0] = aid
                    _await_select[0] = False
                    _post_text(f"↩️ Weiter mit **A{aid}** — {_aufgaben[aid]['title']}")
                    return
        _await_select[0] = False

    # "A1 text", "#1 text", "zu A1 ...", "Aufgabe A1 ..." → zu Aufgabe routen
    m = REF_RE.match(merged_text.strip())
    if not m:
        m2 = REF_ANYWHERE_RE.search(merged_text)
        if m2:
            # Referenz irgendwo im Text — ganzen Text als Fortsetzung schicken
            aid = int(m2.group(1))
            with _aufgaben_lock:
                has = aid in _aufgaben
            if has:
                _cur_aufgabe[0] = aid
                _await_select[0] = False
                threading.Thread(target=_run_task, args=(merged_post, sender_name, aid), daemon=True).start()
                return
    if m:
        aid = int(m.group(1))
        rest = m.group(2).strip()
        with _aufgaben_lock:
            has = aid in _aufgaben
        if has:
            _cur_aufgabe[0] = aid
            _await_select[0] = False
            if not rest:
                with _aufgaben_lock:
                    title = _aufgaben[aid]["title"]
                _post_text(f"↩️ Weiter mit **A{aid}** — {title}")
                return
            merged_post["message"] = rest
            threading.Thread(target=_run_task, args=(merged_post, sender_name, aid), daemon=True).start()
            return

    # "andere Aufgabe: ..." → neue Session eroeffnen
    m = NEUE_RE.search(low)
    if m:
        after = merged_text[m.end():].strip().lstrip(":- ").strip()
        title = after.splitlines()[0][:50] if after else "Neue Aufgabe"
        aid = _open_aufgabe(title)
        _post_text(f"📋 **A{aid}** eroeffnet — _{title}_")
        if after:
            merged_post["message"] = after
            threading.Thread(target=_run_task, args=(merged_post, sender_name, aid), daemon=True).start()
        return

    # Normal: zu aktueller Aufgabe — nur beim allerersten Mal auto-anlegen
    with _aufgaben_lock:
        aid = _cur_aufgabe[0]
    if aid is None:
        title = merged_text.splitlines()[0][:50] if merged_text else "Aufgabe"
        aid = _open_aufgabe(title)
    threading.Thread(target=_run_task, args=(merged_post, sender_name, aid), daemon=True).start()


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
    user_id = post.get("user_id", sender_name)
    with _debounce_lock:
        if user_id in _debounce_buffers:
            # Timer zurücksetzen, Nachricht anhängen
            _debounce_buffers[user_id]["timer"].cancel()
            _debounce_buffers[user_id]["posts"].append(post)
        else:
            _debounce_buffers[user_id] = {"posts": [post], "sender_name": sender_name}
        t = threading.Timer(DEBOUNCE_SECONDS, _flush_debounce, args=(user_id,))
        _debounce_buffers[user_id]["timer"] = t
        t.daemon = True
        t.start()


def main():
    global driver, BOT_USER_ID
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(OUTBOX, exist_ok=True)
    _load_state()
    active = _get_active()
    log.info("State geladen: aktiv=%s (%s) → %s", active["name"], active["type"], active["dir"])
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
