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
MM_CHANNEL_ID_MAILCENTER = os.environ.get("MM_CHANNEL_ID_MAILCENTER", "")
ALLOWED_CHANNEL_IDS = {MM_CHANNEL_ID} | ({MM_CHANNEL_ID_MAILCENTER} if MM_CHANNEL_ID_MAILCENTER else set())
MM_SCHEME = os.environ.get("MM_SCHEME", "https")
MM_PORT = int(os.environ.get("MM_PORT", "443"))
MM_OWNER = os.environ.get("MM_OWNER_USER_ID", "")
MM_DM_CHANNEL_ID = os.environ.get("MM_DM_CHANNEL_ID", "")
CHANNEL_NAME = os.environ.get("DEMOBOT_CHANNEL_NAME", "demobot")
MAX_PARALLEL = int(os.environ.get("DEMOBOT_MAX_PARALLEL", "5"))
AUFGABEN_MAX = int(os.environ.get("DEMOBOT_MAX_AUFGABEN", "2"))
SESSION_TTL = int(os.environ.get("DEMOBOT_SESSION_TTL", str(2 * 3600)))  # Default 2h

API_BASE = f"{MM_SCHEME}://{MM_URL}/api/v4"
AUTH_H = {"Authorization": "Bearer " + MM_TOKEN}
DEMOBOT_DIR = core.dir_for(CHANNEL_NAME)

_channel_type_cache = {}  # channel_id -> 'O'/'P'/'D'/'G'

def _get_channel_type(channel_id):
    if channel_id in _channel_type_cache:
        return _channel_type_cache[channel_id]
    try:
        r = requests.get(f"{API_BASE}/channels/{channel_id}", headers=AUTH_H, timeout=10)
        ctype = r.json().get("type", "O")
    except Exception:
        ctype = "O"
    _channel_type_cache[channel_id] = ctype
    return ctype
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
                files_in=None, files_out=None, status="fertig", aufgabe_id=None,
                dauer_s=None, session_resets=None):
    os.makedirs(os.path.dirname(DIALOG_LOG), exist_ok=True)
    active_dir = active_dir or DEMOBOT_DIR
    active = _get_active()
    entry = {
        "ts": datetime.datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": core.MACHINE,
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
        "dauer_s": dauer_s,
        "session_resets": session_resets,
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
_ws_last_activity = [0.0]   # Timestamp letzter WS-Message; 0 = noch nie
_ws_ping_started = [False]
_tasks = {}
_task_lock = threading.Lock()
_task_seq = [0]
_auth_lock    = threading.Lock()
_auth_pending = False
_auth_proc    = None

# --- Multi-Aufgaben-State ---
_aufgaben = {}          # int -> {id, title, session_key, status}
_aufgabe_seq = [0]
_cur_aufgabe = [None]   # aktuell aktive Aufgabe-ID (None = Einzel-Modus)
_await_select = [False] # True = warte auf A1/A2/... nach "zurueck"
_aufgaben_lock = threading.Lock()
_STARTUP_TS = int(time.time() * 1000)  # ms — Nachrichten vor Neustart ignorieren

# Aufgaben ueberleben Neustart (sonst kippt alles zurueck auf A1).
_AUFGABEN_FILE = os.path.join(DEMOBOT_DIR, "_aufgaben.json")


def _save_aufgaben():
    try:
        with _aufgaben_lock:
            data = {"aufgaben": {str(k): v for k, v in _aufgaben.items()},
                    "cur": _cur_aufgabe[0], "seq": _aufgabe_seq[0]}
        with open(_AUFGABEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        log.warning("Aufgaben speichern fehlgeschlagen", exc_info=True)


def _load_aufgaben():
    if not os.path.exists(_AUFGABEN_FILE):
        return
    try:
        with open(_AUFGABEN_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        with _aufgaben_lock:
            _aufgaben.clear()
            for k, v in (data.get("aufgaben") or {}).items():
                _aufgaben[int(k)] = v
            _cur_aufgabe[0] = data.get("cur")
            _aufgabe_seq[0] = int(data.get("seq", 0))
        log.info("Aufgaben geladen: %d (cur=%s, seq=%d)",
                 len(_aufgaben), _cur_aufgabe[0], _aufgabe_seq[0])
    except Exception:
        log.warning("Aufgaben laden fehlgeschlagen", exc_info=True)


def _get_aufgabe(aid):
    with _aufgaben_lock:
        a = _aufgaben.get(aid)
        return dict(a) if a else None


def _aufgabe_by_root(root_id):
    """Aufgabe-ID anhand Thread-Root oder Status-Post-ID finden."""
    if not root_id:
        return None
    with _aufgaben_lock:
        for aid, a in _aufgaben.items():
            if a.get("root_id") == root_id or a.get("main_post_id") == root_id:
                return aid
    return None

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

MODEL_MAP = {
    "opus":   "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}
_MODEL_TRIGGER = re.compile(r'\b(opus|sonnet|haiku)\b', re.I)
_MODEL_CONTEXT = re.compile(
    r'model(?:l)?|stell|wechsel|switch|umstell|'
    r'\bauf\s+(?:opus|sonnet|haiku)\b|'
    r'\bzu\s+(?:opus|sonnet|haiku)\b', re.I)

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

SPINNER_FRAMES = ["/", "-", "\\", "|"]


_PID_FILE = os.path.join(DEMOBOT_DIR, ".pid")


def _acquire_pidlock():
    """Verhindert doppelte Prozesse. Alten Prozess per psutil beenden."""
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            import psutil
            try:
                p = psutil.Process(old_pid)
                cmd = " ".join(p.cmdline())
                if "demobot_mm" in cmd:
                    log.info("Alter demobot-Prozess gefunden (PID %d) — beende ihn", old_pid)
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except Exception:
                        p.kill()
                    log.info("Alter Prozess beendet")
            except Exception:
                pass  # NoSuchProcess oder psutil nicht verfügbar
        except Exception as e:
            log.warning("PID-Lock prüfen fehlgeschlagen: %s", e)
    with open(_PID_FILE, "w") as fh:
        fh.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.unlink(_PID_FILE) if os.path.exists(_PID_FILE) else None)
    log.info("PID-Lock gesetzt: PID %d", os.getpid())


def _extract_code(text):
    """Extrahiert Auth-Code aus URL oder direkt als Code."""
    import urllib.parse
    text = text.strip()
    if "code=" in text:
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
            code = params.get("code", [None])[0]
            if code:
                return code
        except Exception:
            pass
    if len(text) > 8 and " " not in text:
        return text
    return None


def _is_auth_ok():
    """Prüft ob Claude-Token noch gültig (schneller Minimal-Call)."""
    claude_cmd = os.environ.get("CLAUDE_CMD", "claude")
    env = {**os.environ}
    try:
        r = __import__("subprocess").run(
            [claude_cmd, "--permission-mode", "bypassPermissions",
             "--output-format", "json", "-p", "x"],
            capture_output=True, text=True, timeout=20, env=env)
        out = r.stdout + r.stderr
        return "401" not in out and "Invalid authentication" not in out
    except Exception:
        return False


def _start_mm_auth():
    """Startet `claude auth login`, liest URL, postet sie in den Kanal."""
    global _auth_proc, _auth_pending
    with _auth_lock:
        if _auth_pending:
            _post_text("⏳ Login läuft bereits — warte auf deinen Code.")
            return
    claude_cmd = os.environ.get("CLAUDE_CMD", "claude")
    import subprocess as _sp
    try:
        proc = _sp.Popen(
            [claude_cmd, "auth", "login"],
            stdin=_sp.PIPE, stdout=_sp.PIPE,
            stderr=_sp.STDOUT, text=True)
    except Exception as e:
        _post_text(f"❌ Auth-Start fehlgeschlagen: {e}")
        return
    url = None
    try:
        for line in iter(proc.stdout.readline, ""):
            if "visit:" in line.lower() or "https://" in line:
                url = line.strip().split()[-1]
                break
    except Exception:
        pass
    if not url:
        proc.kill()
        _post_text("❌ Login-URL konnte nicht gelesen werden — starte Bot neu.")
        return
    with _auth_lock:
        _auth_proc = proc
        _auth_pending = True
    _post_text(
        "🔑 **Claude-Login erforderlich**\n\n"
        "Öffne diesen Link auf dem Handy:\n\n"
        + url + "\n\n"
        "Nach dem Login bekommst du einen Code — einfach hier eintippen."
    )
    log.info("Auth-URL in Kanal gepostet: %s", url)


def _complete_mm_auth(code_text):
    """Füttert den Auth-Code an den laufenden `claude auth login` Prozess."""
    global _auth_proc, _auth_pending
    code = _extract_code(code_text)
    if not code:
        _post_text("❌ Code nicht erkannt — nur den Code eingeben, kein Leerzeichen.")
        return
    with _auth_lock:
        proc = _auth_proc
    if not proc:
        _post_text("❌ Kein Auth-Prozess aktiv — schreibe `neu einloggen` zum Neustart.")
        return
    try:
        proc.stdin.write(code + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        ret = proc.wait(timeout=15)
        with _auth_lock:
            _auth_pending = False
            _auth_proc = None
        if ret == 0:
            _post_text("✅ **Login erfolgreich!** Bot läuft wieder normal.")
            log.info("Auth-Flow abgeschlossen.")
        else:
            _post_text(f"❌ Login fehlgeschlagen (Exit {ret}) — nochmal versuchen.")
    except Exception as e:
        _post_text(f"❌ Auth-Fehler: {e}")
        with _auth_lock:
            _auth_pending = False
            _auth_proc = None


def _make_driver():
    return Driver({"url": MM_URL, "token": MM_TOKEN, "scheme": MM_SCHEME,
                   "port": MM_PORT, "verify": True, "timeout": 30})


def _post_text(text, channel_id=None, root_id=None):
    cid = channel_id or MM_CHANNEL_ID
    text = text or "(leer)"
    for i in range(0, len(text), 16000):
        body = {"channel_id": cid, "message": text[i:i + 16000]}
        if root_id:
            body["root_id"] = root_id
        driver.posts.create_post(body)


def _create_post(msg, channel_id=None, root_id=None):
    cid = channel_id or MM_CHANNEL_ID
    body = {"channel_id": cid, "message": msg}
    if root_id:
        body["root_id"] = root_id
    last_exc = None
    for attempt in range(3):
        try:
            return driver.posts.create_post(body)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 401:
                raise  # MM-Token ungültig — nicht retrybar
            last_exc = e
            if attempt < 2:
                time.sleep(2 ** attempt)
    if root_id:
        try:
            return driver.posts.create_post({"channel_id": cid, "message": msg})
        except Exception:
            pass
    raise last_exc


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


def _post_file(path, channel_id=None, root_id=None):
    cid = channel_id or MM_CHANNEL_ID
    with open(path, "rb") as f:
        r = requests.post(f"{API_BASE}/files", headers=AUTH_H,
                          data={"channel_id": cid},
                          files={"files": (os.path.basename(path), f)}, timeout=180)
    r.raise_for_status()
    fid = r.json()["file_infos"][0]["id"]
    body = {"channel_id": cid, "message": "", "file_ids": [fid]}
    if root_id:
        body["root_id"] = root_id
    driver.posts.create_post(body)


def _next_tid():
    with _task_lock:
        _task_seq[0] += 1
        tid = _task_seq[0]
    _save_state()
    return tid


def _render_live(tk):
    icon = {"läuft": "▶️", "fertig": "✅", "abgebrochen": "⏹️", "fehler": "❌"}.get(tk["status"], "▶️")
    spin = f" `{tk.get('spinner', '')}`" if tk["status"] == "läuft" else ""
    num = tk.get("label_num") or f"#{tk['id']}"
    head = f"{icon} **{num}**{tk.get('label', '')}{spin} {tk['status']} — _{tk['title']}_"
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


def _open_aufgabe(title, work_dir=None, name=None, root_id=None):
    active = _get_active()
    with _aufgaben_lock:
        _aufgabe_seq[0] += 1
        aid = _aufgabe_seq[0]
        _aufgaben[aid] = {"id": aid, "title": title[:50],
                          "session_key": f"mm_{root_id}" if root_id else f"{CHANNEL_NAME}_A{aid}",
                          "status": "aktiv",
                          "dir": work_dir or active["dir"],
                          "name": name or active["name"],
                          "root_id": root_id,
                          "sub_seq": 0,
                          "main_post_id": None}
        _cur_aufgabe[0] = aid
    _save_aufgaben()
    return aid


def _next_sub(aufgabe_id):
    with _aufgaben_lock:
        _aufgaben[aufgabe_id]["sub_seq"] = _aufgaben[aufgabe_id].get("sub_seq", 0) + 1
        return _aufgaben[aufgabe_id]["sub_seq"]


def _update_aufgaben_post(aufgabe_id, status, preview=None):
    """Legt Hauptstrang-Post an (beim ersten Mal) oder editiert ihn."""
    a = _get_aufgabe(aufgabe_id)
    if not a:
        return
    icons = {"QUEUED": "🕐", "AKTUELL": "▶️", "DONE": "✅", "FEHLER": "❌"}
    icon = icons.get(status, "•")
    proj = a.get("name", CHANNEL_NAME)
    sub = a.get("sub_seq", 0)
    antworten = f"{sub} Antwort{'en' if sub != 1 else ''}"
    laufend = f"läuft #{aufgabe_id}.{sub}" if status == "AKTUELL" else antworten
    text = f"{icon} **#{aufgabe_id}** [{proj}] **{status}** — {laufend}"
    if preview and status == "DONE":
        kurz = preview[:120].replace("\n", " ")
        text += f"\n   ↳ _{kurz}_"
    with _aufgaben_lock:
        post_id = _aufgaben.get(aufgabe_id, {}).get("main_post_id")
    if post_id:
        _patch(post_id, text)
    else:
        post = _create_post(text, MM_CHANNEL_ID)
        with _aufgaben_lock:
            if aufgabe_id in _aufgaben:
                _aufgaben[aufgabe_id]["main_post_id"] = post["id"]
        _save_aufgaben()


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


def _process(text, files, sender="user", aufgabe_id=None, reply_channel_id=None):
    a = _get_aufgabe(aufgabe_id) if aufgabe_id else None
    active = _get_active()
    work_dir = (a or {}).get("dir") or active["dir"]
    a_name = (a or {}).get("name") or active["name"]
    thread_root = (a or {}).get("root_id")  # Mattermost-Thread der Aufgabe
    tid = _next_tid()
    session_key = _get_session_key(aufgabe_id)
    proj_label = "" if a_name == CHANNEL_NAME else f" [{a_name}]"
    dir_label = f" `{os.path.basename(work_dir)}`"
    sub = _next_sub(aufgabe_id) if aufgabe_id else None
    label_num = f"#{aufgabe_id}.{sub}" if aufgabe_id and sub else f"#{tid}"
    label = f"{proj_label}{dir_label}"
    title = (text.splitlines()[0][:55] if text else ("Datei-Aufgabe" if files else "Aufgabe"))
    post = _create_post(f"▶️ **{label_num}**{label} läuft … _{title}_", reply_channel_id,
                        root_id=thread_root)
    tk = {"id": tid, "title": title, "status": "läuft", "proc": None,
          "post_id": post["id"],
          "steps": [], "last": 0.0,
          "aufgabe_id": aufgabe_id, "label": label, "label_num": label_num}
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
        if aufgabe_id:
            _update_aufgaben_post(aufgabe_id, "AKTUELL")
        _spin_stop = threading.Event()
        def _run_spinner(stop_ev=_spin_stop, t=tk):
            i = 0
            last_patch = 0.0
            while not stop_ev.wait(0.5):
                t["spinner"] = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
                now = time.time()
                if t["status"] == "läuft" and now - last_patch >= 1.5:
                    _patch(t["post_id"], _render_live(t))
                    last_patch = now
                i += 1
        threading.Thread(target=_run_spinner, daemon=True).start()
        t_start = time.time()
        try:
            aufgabe_filter = f"A{aufgabe_id}" if aufgabe_id else None
            root_id_val = (a or {}).get("root_id") or None
            reply, outfiles = core.run_stream(
                session_key, text, files,
                on_progress=on_progress, on_start=on_start,
                on_notice=lambda m: _post_text(m, reply_channel_id, thread_root),
                work_dir=work_dir, inbox_dir=INBOX, outbox_dir=OUTBOX,
                aufgabe_filter=aufgabe_filter, root_id=root_id_val)
            meta = getattr(core.run_stream, "last_meta", {})
            dauer_s = meta.get("dauer_s", round(time.time() - t_start, 1))
            session_resets = meta.get("session_resets", 0)
            # 401 → Auth-Flow starten
            if reply and ("401" in reply or "Invalid authentication" in reply):
                log.warning("[%s] Claude-401 erkannt — starte MM-Auth-Flow", core.MACHINE)
                threading.Thread(target=_start_mm_auth, daemon=True).start()
            if tk["status"] == "abgebrochen":
                _patch(tk["post_id"], f"⏹️ **{label_num}** abgebrochen — _{title}_")
                _log_dialog(sender, text, None, tid, work_dir, files, [], "abgebrochen", aufgabe_id,
                            dauer_s=dauer_s, session_resets=session_resets)
                return
            tk["status"] = "fertig"
            # SWITCH-Signal auswerten (erste Zeile der Antwort)
            reply_lines = (reply or "").splitlines()
            if reply_lines and reply_lines[0].startswith("SWITCH:"):
                parts = reply_lines[0].strip().split(":")
                if len(parts) == 3:
                    sw_typ, sw_name = parts[1], parts[2].strip()
                    if sw_typ == "projekt":
                        _post_text(_cmd_projekt(sw_name), reply_channel_id, thread_root)
                    elif sw_typ == "vorgang":
                        _post_text(_cmd_vorgang(sw_name), reply_channel_id, thread_root)
                reply = "\n".join(reply_lines[1:]).strip()
            _patch(tk["post_id"], f"✅ **{label_num}**{label} — _{title}_\n\n{(reply or '')[:15000]}")
            if aufgabe_id:
                _update_aufgaben_post(aufgabe_id, "DONE", preview=reply)
            sent = []
            for p in outfiles:
                try:
                    _post_file(p, reply_channel_id, thread_root)
                    sent.append(p)
                    log.info("[%s] Datei gesendet: %s", core.MACHINE, os.path.basename(p))
                except Exception:
                    log.exception("[%s] Konnte Datei nicht senden: %s", core.MACHINE, p)
            if sent:
                core.archive_sent(CHANNEL_NAME, sent)
            log.info("[%s] task DONE aufgabe=A%s dauer=%.1fs resets=%d reply_len=%d",
                     core.MACHINE, aufgabe_id, dauer_s, session_resets, len(reply or ""))
            _log_dialog(sender, text, reply, tid, work_dir, files, sent, "fertig", aufgabe_id,
                        dauer_s=dauer_s, session_resets=session_resets)
            # Aufgabe als fertig markieren
            if aufgabe_id:
                with _aufgaben_lock:
                    if aufgabe_id in _aufgaben:
                        _aufgaben[aufgabe_id]["status"] = "fertig"
                _save_aufgaben()
        except Exception as e:
            dauer_s = round(time.time() - t_start, 1)
            log.exception("[%s] task FEHLER aufgabe=A%s dauer=%.1fs: %s",
                          core.MACHINE, aufgabe_id, dauer_s, e)
            tk["status"] = "fehler"
            _patch(tk["post_id"], f"❌ **{label_num}**{label} Fehler — {e}")
            if aufgabe_id:
                _update_aufgaben_post(aufgabe_id, "FEHLER")
            _log_dialog(sender, text, str(e), tid, work_dir, files, [], "fehler", aufgabe_id,
                        dauer_s=dauer_s)
        finally:
            _spin_stop.set()
            tk["proc"] = None


def _switch_model(name):
    key = name.lower()
    model_id = MODEL_MAP.get(key, key)
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["model"] = model_id
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return f"✅ Modell auf **{model_id}** gestellt — gilt ab der nächsten Anfrage."


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


def _handle_post(post, sender_name, aufgabe_id=None, reply_channel_id=None):
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
        _post_text(_cmd_projekt(m.group(1).strip()), reply_channel_id)
        return
    if low_c in {"/projekt", "/project"}:
        _post_text(_cmd_projekt(""), reply_channel_id)
        return

    # Vorgang wechseln — NUR mit /
    m = re.match(r"^/vorgang\s+(.+)", low_c)
    if m:
        _post_text(_cmd_vorgang(m.group(1).strip()), reply_channel_id)
        return
    if low_c in {"/vorgang"}:
        _post_text(_cmd_vorgang(""), reply_channel_id)
        return

    # Zurück zu demobot
    if low_c in {"/zurück", "/zurueck", "/home"}:
        _post_text(_cmd_zurueck(), reply_channel_id)
        return

    # Deploy: git pull + Service-Neustart
    if low_c in {"/deploy", "/update"}:
        def _do_deploy(rcid=reply_channel_id):
            import subprocess as _sp
            bot_dir = os.path.dirname(os.path.abspath(__file__))
            r = _sp.run(["git", "-C", bot_dir, "pull"],
                        capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                _post_text(f"❌ git pull fehlgeschlagen:\n```\n{(r.stderr or r.stdout)[:400]}\n```", rcid)
                return
            out = (r.stdout or "").strip()
            if "Already up to date" in out:
                _post_text("✅ Bereits aktuell — kein Neustart nötig.", rcid)
                return
            _post_text(f"✅ Update:\n```\n{out[:300]}\n```\n🔄 Neustart in 3s …", rcid)
            time.sleep(3)
            svc = os.environ.get("DEMOBOT_SERVICE_NAME", f"{core.MACHINE}-server-bot")
            nssm_exe = r"C:\tools\nssm\win64\nssm.exe"
            if os.path.exists(nssm_exe):
                _sp.Popen([nssm_exe, "restart", svc])
            else:
                _post_text("⚠️ nssm nicht gefunden — bitte manuell neu starten.", rcid)
        threading.Thread(target=_do_deploy, daemon=True).start()
        _post_text("🔄 Prüfe auf Updates …", reply_channel_id)
        return

    # Status
    if low_c in STATUS_WORDS or low in STATUS_WORDS:
        _post_text(_list_tasks(), reply_channel_id)
        return

    # Stop
    m = re.match(r"(?:stop|unterbrich|abbrechen|abbruch|halt)\s*#?(\d+)", low)
    if m:
        _post_text(_stop(int(m.group(1))), reply_channel_id)
        return

    # Model-Switch: "auf opus", "modell wechseln zu haiku", etc.
    m = _MODEL_TRIGGER.search(low)
    if m and _MODEL_CONTEXT.search(low):
        _post_text(_switch_model(m.group(1)), reply_channel_id)
        return

    if not text and not incoming:
        return

    _check_and_reset_ttl()
    _touch_activity()
    active = _get_active()
    log.info("[demobot:%s] %s: %s %s", active["name"], sender_name, text,
             f"[+{len(incoming)} Datei]" if incoming else "")
    _process(text, incoming, sender=sender_name, aufgabe_id=aufgabe_id, reply_channel_id=reply_channel_id)


def _run_task(post, sender, aufgabe_id=None, reply_channel_id=None):
    try:
        _handle_post(post, sender, aufgabe_id=aufgabe_id, reply_channel_id=reply_channel_id)
    except Exception as e:
        log.exception("Fehler bei der Verarbeitung")
        try:
            _post_text(f"❌ Interner Fehler: {e}", reply_channel_id)
        except Exception:
            pass


# Debounce: Nachrichten pro Sender sammeln, 3s warten, dann zusammenführen.
# Verhindert dass Spracheingabe-Fragmente einzeln verarbeitet werden.
DEBOUNCE_SECONDS = float(os.environ.get("DEMOBOT_DEBOUNCE", "3.0"))
_debounce_lock = threading.Lock()
_debounce_buffers = {}   # sender_id -> {"timer": Timer, "posts": [...], "sender_name": str, "files": [...]}


def _flush_debounce(key):
    with _debounce_lock:
        buf = _debounce_buffers.pop(key, None)
    if not buf:
        return
    posts = buf["posts"]
    sender_name = buf["sender_name"]
    rcid = buf.get("reply_channel_id")  # None → Hauptkanal
    root_id = buf.get("root_id") or ""
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

    # Thread-Routing: schreibt der User in einem Aufgaben-Thread? -> direkt dorthin.
    if root_id:
        aid = _aufgabe_by_root(root_id)
        if aid is not None:
            with _aufgaben_lock:
                _cur_aufgabe[0] = aid
                # Wenn User auf Status-Post geantwortet hat → Thread dorthin verlegen
                if _aufgaben.get(aid, {}).get("main_post_id") == root_id:
                    _aufgaben[aid]["root_id"] = root_id
            _save_aufgaben()
            _update_aufgaben_post(aid, "QUEUED")
            threading.Thread(target=_run_task, args=(merged_post, sender_name, aid),
                             kwargs={"reply_channel_id": rcid}, daemon=True).start()
            return
        # Thread gehoert zu keiner Aufgabe (fremder Thread) -> normale Logik

    low = merged_text.lower().strip()

    # "zurueck" / "was laeuft" / "aufgaben" → Liste zeigen, auf Auswahl warten
    if re.match(r"^(zur[uü]ck|zurueck|back|was l[aä]uft|aufgaben|welche aufgabe)", low):
        with _aufgaben_lock:
            hat_aufgaben = bool(_aufgaben)
        if hat_aufgaben:
            _post_text(_aufgaben_liste(), rcid)
            _await_select[0] = True
            return
        # keine Aufgaben → normal verarbeiten
        threading.Thread(target=_run_task, args=(merged_post, sender_name), kwargs={"reply_channel_id": rcid}, daemon=True).start()
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
                    _post_text(f"↩️ Weiter mit **A{aid}** — {_aufgaben[aid]['title']}", rcid)
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
                _update_aufgaben_post(aid, "QUEUED")
                threading.Thread(target=_run_task, args=(merged_post, sender_name, aid), kwargs={"reply_channel_id": rcid}, daemon=True).start()
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
                _post_text(f"↩️ Weiter mit **A{aid}** — {title}", rcid)
                return
            merged_post["message"] = rest
            _update_aufgaben_post(aid, "QUEUED")
            threading.Thread(target=_run_task, args=(merged_post, sender_name, aid), kwargs={"reply_channel_id": rcid}, daemon=True).start()
            return

    # "andere Aufgabe: ..." → neue Session eroeffnen
    m = NEUE_RE.search(low)
    if m:
        after = merged_text[m.end():].strip().lstrip(":- ").strip()
        title = after.splitlines()[0][:50] if after else "Neue Aufgabe"
        aid = _open_aufgabe(title, root_id=merged_post.get("id"))
        _post_text(f"📋 **A{aid}** eroeffnet — _{title}_", rcid, merged_post.get("id"))
        _update_aufgaben_post(aid, "QUEUED")
        if after:
            merged_post["message"] = after
            threading.Thread(target=_run_task, args=(merged_post, sender_name, aid), kwargs={"reply_channel_id": rcid}, daemon=True).start()
        return

    # Normal: Top-Level-Hauptkanal → immer neue Aufgabe (eigener Thread pro Thema).
    # Thread-Antworten (root_id gesetzt) wurden oben schon abgefangen.
    title = merged_text.splitlines()[0][:50] if merged_text else "Aufgabe"
    aid = _open_aufgabe(title, root_id=merged_post.get("id"))
    _update_aufgaben_post(aid, "QUEUED")
    threading.Thread(target=_run_task, args=(merged_post, sender_name, aid), kwargs={"reply_channel_id": rcid}, daemon=True).start()


def _ws_ping_loop():
    """Sendet alle 25s einen Ping-Frame in den WebSocket.

    Verhindert NAT-Zombie: Router verwirft idle TCP-Verbindungen nach ~60-90s.
    Ein Ping alle 25s haelt den NAT-Eintrag am Leben, so dass der Watchdog
    nur bei echtem Ausfall auslöst (nicht bei Idle).
    """
    import asyncio as _asyncio
    INTERVAL = 25
    seq = [0]
    while True:
        time.sleep(INTERVAL)
        try:
            ws_obj = getattr(getattr(driver, "client", None), "websocket", None)
            if ws_obj is None:
                continue
            loop = getattr(ws_obj, "_loop", None)
            if loop is None or loop.is_closed():
                continue
            seq[0] += 1
            msg = json.dumps({"action": "ping", "seq": seq[0]})
            async def _send(m=msg):
                await ws_obj.send(m)
            fut = _asyncio.run_coroutine_threadsafe(_send(), loop)
            fut.result(timeout=5)
            _ws_last_activity[0] = time.time()  # Ping zaehlt als Aktivitaet
            log.debug("WS ping #%d", seq[0])
        except Exception as e:
            log.debug("WS ping fehlgeschlagen: %s", e)


async def event_handler(message):
    _ws_last_activity[0] = time.time()   # jede WS-Nachricht = Verbindung lebt
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
    channel_id = post.get("channel_id", "")
    user_id = post.get("user_id", "")

    # Erlaubte Kanäle oder DM-Kanal vom Owner
    if channel_id in ALLOWED_CHANNEL_IDS:
        reply_channel_id = channel_id if channel_id != MM_CHANNEL_ID else None
    else:
        if MM_OWNER and user_id != MM_OWNER:
            return
        if _get_channel_type(channel_id) != "D":
            return
        reply_channel_id = channel_id
        log.info("[demobot] DM von %s in Kanal %s", user_id, channel_id)

    if user_id == BOT_USER_ID:
        return
    # System-Meldungen (join/leave/add/header…) ignorieren — kein Input
    if (post.get("type") or "").startswith("system_"):
        return
    if post.get("create_at", 0) < _STARTUP_TS:
        return  # Nachricht von vor dem Neustart — ignorieren
    # Auth-Flow: wenn _auth_pending → User-Eingabe als Code interpretieren
    if _auth_pending:
        code_candidate = (post.get("message") or "").strip()
        if _extract_code(code_candidate):
            threading.Thread(target=_complete_mm_auth, args=(code_candidate,), daemon=True).start()
            return
    sender_name = (data["data"].get("sender_name") or "").lstrip("@") or "user"
    # Debounce pro (User, Thread): Nachrichten aus verschiedenen Threads NICHT mischen.
    root_id = post.get("root_id") or ""
    key = (user_id, root_id)
    with _debounce_lock:
        if key in _debounce_buffers:
            # Timer zurücksetzen, Nachricht anhängen
            _debounce_buffers[key]["timer"].cancel()
            _debounce_buffers[key]["posts"].append(post)
        else:
            _debounce_buffers[key] = {"posts": [post], "sender_name": sender_name,
                                      "reply_channel_id": reply_channel_id, "root_id": root_id}
        t = threading.Timer(DEBOUNCE_SECONDS, _flush_debounce, args=(key,))
        _debounce_buffers[key]["timer"] = t
        t.daemon = True
        t.start()


def main():
    _acquire_pidlock()
    global driver, BOT_USER_ID
    os.makedirs(INBOX, exist_ok=True)
    os.makedirs(OUTBOX, exist_ok=True)
    _load_state()
    _load_aufgaben()
    active = _get_active()
    log.info("State geladen: aktiv=%s (%s) → %s", active["name"], active["type"], active["dir"])
    if not _ws_ping_started[0]:
        _ws_ping_started[0] = True
        threading.Thread(target=_ws_ping_loop, daemon=True).start()
        log.info("WS-Ping gestartet (interval=25s)")
    while True:
        try:
            driver = _make_driver()
            driver.login()
            me = driver.users.get_user("me")
            BOT_USER_ID = me["id"]
            log.info("Verbunden als @%s | Kanal %s -> %s | Maschine %s | Owner-only:%s",
                     me.get("username"), CHANNEL_NAME, core.dir_for(CHANNEL_NAME),
                     core.MACHINE, bool(MM_OWNER))
            if not _is_auth_ok():
                log.warning("Claude-Auth ungültig — starte MM-Auth-Flow")
                threading.Thread(target=_start_mm_auth, daemon=True).start()
            _ws_last_activity[0] = time.time()  # reset bei neuer Verbindung
            driver.init_websocket(event_handler)
            log.warning("WebSocket beendet — reconnect in 5s")
        except Exception:
            log.exception("Verbindungsfehler — reconnect in 5s")
        time.sleep(5)


if __name__ == "__main__":
    main()
