# -*- coding: utf-8 -*-
"""
demobot_core.py — Windows Claude CLI Engine
Based on bot-core v1.6 (github.com/dw-hub-bot/bot-core)

3-layer resilience, session management, pre-compact backup, proactive ctx reset.

Config via environment:
  DEMOBOT_BASE_DIR         Bot instance dir (default: C:\projekte\demobot)
  DEMOBOT_BASE             Projects root    (default: C:\projekte)
  BOT_NAME                 Display name     (default: demobot)
  CLAUDE_CMD               Path to claude
  CLAUDE_MODEL             Model            (default: claude-sonnet-4-6)
  BOT_CTX_LIMIT_BYTES      Proactive reset threshold (default: 2 MB)
  BOT_BACKUP_MIN_BYTES     Min .jsonl size to backup (default: 100 KB)
  BOT_BACKUP_KEEP          Session backups to keep   (default: 5)
  DEMOBOT_SILENCE_TIMEOUT  Silence watchdog (default: 600 s)
  DEMOBOT_MAX_TIMEOUT      Absolute max     (default: 3600 s)
"""

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


# ── Elevated-Privileges Guard (bot-core v1.6.1) ───────────────────────────────
def _is_elevated() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes, ctypes.wintypes
            TOKEN_QUERY = 0x0008
            TokenElevation = 20
            h = ctypes.wintypes.HANDLE()
            if not ctypes.windll.advapi32.OpenProcessToken(
                ctypes.windll.kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h)
            ):
                return False
            elev = ctypes.c_ulong(0)
            sz = ctypes.c_ulong(ctypes.sizeof(elev))
            ctypes.windll.advapi32.GetTokenInformation(
                h, TokenElevation, ctypes.byref(elev), sz, ctypes.byref(sz)
            )
            ctypes.windll.kernel32.CloseHandle(h)
            return elev.value != 0
        except Exception:
            return False
    return os.geteuid() == 0

if _is_elevated():
    print("FATAL: demobot darf NICHT als Administrator laufen! "
          "Claude blockiert bypassPermissions unter Admin.",
          file=sys.stderr)
    sys.exit(78)


try:
    from dotenv import load_dotenv
    _env_base = Path(os.getenv("DEMOBOT_BASE_DIR", Path(__file__).parent))
    load_dotenv(_env_base / ".env")
except ImportError:
    pass

BOT_DIR       = Path(os.getenv("DEMOBOT_BASE_DIR", r"C:\projekte\demobot"))
BOT_NAME      = os.getenv("BOT_NAME", "demobot")
CLAUDE_CMD    = os.getenv(
    "CLAUDE_CMD",
    r"C:\Users\Lenovo T460p\AppData\Roaming\npm\claude.cmd")
CLAUDE_MODEL  = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
PROJ_ROOT     = os.getenv("DEMOBOT_BASE",          r"C:\projekte")
BASE_DIR      = PROJ_ROOT   # Compat-Alias fuer demobot_mm.py (core.BASE_DIR = Projekte-Root)
VORGANG_BASE  = os.getenv("DEMOBOT_VORGANG_BASE",
                r"G:\Meine Ablage\ESALOW-Archiv\_vorgaenge")
CLAUDE_META   = os.getenv("DEMOBOT_CLAUDE_META",   r"C:\projekte\claude-meta")
LAST_CALL_FILE = (
    Path(os.environ.get("USERPROFILE", r"C:\Users\Lenovo T460p"))
    / ".claude" / "last_claude_call.txt"
)

CTX_LIMIT_BYTES  = int(os.getenv("BOT_CTX_LIMIT_BYTES",  str(2 * 1024 * 1024)))
BACKUP_MIN_BYTES = int(os.getenv("BOT_BACKUP_MIN_BYTES", str(100 * 1024)))
BACKUP_KEEP      = int(os.getenv("BOT_BACKUP_KEEP",      "5"))
SILENCE_TIMEOUT  = int(os.getenv("DEMOBOT_SILENCE_TIMEOUT", "600"))
MAX_TIMEOUT      = int(os.getenv("DEMOBOT_MAX_TIMEOUT",     "3600"))

BACKUP_DIR = BOT_DIR / "session_backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
(BOT_DIR / "logs").mkdir(parents=True, exist_ok=True)

log     = logging.getLogger("demobot-core")
MACHINE = os.environ.get("DEMOBOT_MACHINE") or socket.gethostname()

_session_lock = threading.Lock()
_active_proc: dict = {}  # channel_key -> Popen (for cancel_run)


# ── System Prompt ─────────────────────────────────────────────────────────────

_APPEND_SYSTEM_TMPL = (
    "KONTEXT: Du wirst per Mattermost gesteuert. Nachrichten kommen oft von Sprach-"
    "Transkription (Handy-Mikrofon) — sie koennen unpraezise, umgangssprachlich oder "
    "fehlerhaft transkribiert sein. Interpretiere semantisch, nicht woertlich. "
    "Die Telefonzentrale (demobot-Kanal) ist KEIN Arbeitsverzeichnis. "
    "Aktuelles Arbeitsverzeichnis: '{work_dir}'. Dort gehoeren dauerhafte Ergebnisse hin. "
    "Uploads des Users: {inbox_dir}. "
    "Was sofort in den Chat soll: nach {outbox_dir} kopieren (wird automatisch gesendet). "
    "Bei Unklarheit kurz fragen ob Ergebnis Chat oder Projekt. "
    "PROJEKT-WECHSEL: Wenn der User erkennbar ein anderes Projekt oder einen Vorgang "
    "oeffnen/wechseln moechte, schreibe als ERSTE Zeile deiner Antwort exakt: "
    "SWITCH:projekt:verzeichnisname ODER SWITCH:vorgang:NAME — dann normaler Text. "
    "Den Verzeichnisnamen aus dem Kontext ableiten (z.B. 'umsatz grizzly' -> 'umsatz_grizzly', "
    "'ohm 73' -> 'OHM73'). Nur SWITCH schreiben wenn eindeutig ein Wechsel gewuenscht ist. "
    "Quellen-Registry: {sources_json} — lies wenn du wissen willst wo Daten liegen. "
    "Projekt-Registry (Metadaten, unvollstaendig): {project_registry} "
    "ALLE Projekte: Verzeichnis C:\\projekte\\ auflisten (list_directory oder glob). "
    "Bei Projekt-Fragen immer das echte Verzeichnis nehmen, Registry nur fuer Metadaten. "
    "WICHTIG: Keine blockierenden Wartezeiten. Antworte auf Deutsch mit echten Umlauten. "
    "IMMER-ANTWORTEN-REGEL: Schreibe zu JEDER Aufgabe einen sichtbaren Text — auch wenn du "
    "nur Tool-Calls ausfuehrst. Niemals die Aufgabe abschliessen ohne erklaerenden Begleittext "
    "(was wurde getan, was ist das Ergebnis, wie geht es weiter). Kein stilles Abarbeiten. "
    "SICHERHEIT STUFE 1 — Doppelte Bestaetigung erforderlich (niemals sofort ausfuehren): "
    "Laptop/PC herunterfahren, neu starten, ausschalten (shutdown, reboot, ausmachen, ausschalten). "
    "Vorgehen: 1) Zusammenfassen was du verstanden hast, 2) Fragen 'Bestaetigung erforderlich — antworte JA'. "
    "Erst nach explizitem JA in der naechsten Nachricht ausfuehren. "
    "SICHERHEIT STUFE 2 — Dienste stoppen/deinstallieren (NSSM stop/delete, sc stop, taskkill): "
    "Eigene/bekannte Dienste (mm-inventar-bot, teiledatenbank-bot, priv-inventar-bot, "
    "mailcenter, discord-privat-bot und andere Projekte aus C:\\projekte\\): einfach ausfuehren. "
    "Ausnahme: den demobot-Dienst selbst (dieser laufende Bot-Prozess) niemals ohne "
    "Bestaetigung stoppen — er wuerde sich selbst wegschliessen und koennte nicht mehr antworten. "
    "Bei 'demobot stoppen': warnen dass der Bot danach nicht mehr erreichbar ist, dann JA abwarten. "
    "Unbekannter Dienst oder Name unklar: ZUERST recherchieren was dieser Dienst ist "
    "(sc query, tasklist, Web-Suche falls noetig), dann kurz erklaeren was der Dienst tut, "
    "dann Bestaetigung einholen bevor gestoppt wird. "
    "VPS-ZUGRIFF: Per SSH ueber Headscale-Mesh erreichbar als 'hetzner-vps'. "
    "TEILEDATENBANK: Laeuft EXCLUSIV auf VPS. "
    "Zugriff: ssh hetzner-vps sqlite3 /opt/priv-inventar-bot/teiledatenbank.db .timeout 5000 SQL. "
    "VPS-Dienste verwalten: ssh hetzner-vps systemctl restart/stop/status DIENST. "
    "NIEMALS lokal starten: priv-inventar-bot, villa-manager, fahrkartenbot. "
    "demobot selbst laeuft NUR lokal — nie auf VPS deployen."
)


def _build_system_prompt(channel: str, work_dir: str,
                         inbox_dir: str, outbox_dir: str,
                         extra_append: str = "") -> str:
    sources  = Path(CLAUDE_META) / "sources.json"
    registry = Path(CLAUDE_META) / "project_registry.md"
    prompt = _APPEND_SYSTEM_TMPL.format(
        work_dir=work_dir, inbox_dir=inbox_dir, outbox_dir=outbox_dir,
        sources_json=str(sources), project_registry=str(registry),
    )
    prompt += (
        f" Du laeufst auf Maschine '{MACHINE}', Kanal '{channel}', "
        f"Arbeitsverzeichnis '{work_dir}'. Wenn jemand fragt WER oder WO "
        f"(welcher Rechner) eine Aufgabe bearbeitet hat, nenne diese Maschine: '{MACHINE}'."
    )
    if extra_append:
        prompt += " " + extra_append
    # Force single line — Windows claude.cmd splits on newlines -> falls back to plaintext
    return " ".join(prompt.split())


# ── Directory helpers ─────────────────────────────────────────────────────────

def dir_for(channel: str) -> str:
    return os.path.join(PROJ_ROOT, channel)


def _ensure(d: str):
    os.makedirs(os.path.join(d, "_inbox"),  exist_ok=True)
    os.makedirs(os.path.join(d, "_outbox"), exist_ok=True)


# ── Session Management ────────────────────────────────────────────────────────

def _session_file(d: str) -> str:
    return os.path.join(d, ".sessions.json")


def _load_session(d: str, channel: str):
    """Return session_id string for channel in directory d, or None."""
    with _session_lock:
        f = _session_file(d)
        if not os.path.exists(f):
            return None
        try:
            data = json.load(open(f, encoding="utf-8"))
            val = data.get(channel)
            if isinstance(val, dict):
                return val.get("session_id") or None
            return val or None
        except Exception:
            return None


def _save_session(d: str, channel: str, sid):
    """Save session_id string (or None) for channel in directory d."""
    with _session_lock:
        f = _session_file(d)
        data = {}
        if os.path.exists(f):
            try:
                data = json.load(open(f, encoding="utf-8"))
            except Exception:
                pass
        # Normalize any dict entries from old bot_core format
        for k, v in list(data.items()):
            if isinstance(v, dict):
                data[k] = v.get("session_id") or None
        data[channel] = sid
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)


def _proj_slug(d: str) -> str:
    """C:\\projekte\\demobot -> C--projekte-demobot"""
    return re.sub(r"[^a-zA-Z0-9]", "-", d)


def _transcript_path(d: str, sid: str) -> Path:
    return Path.home() / ".claude" / "projects" / _proj_slug(d) / f"{sid}.jsonl"


def _session_size(d: str, sid: str) -> int:
    """Return bytes of Claude transcript, 0 if missing."""
    if not sid:
        return 0
    try:
        return _transcript_path(d, sid).stat().st_size
    except Exception:
        return 0


def _backup_session_jsonl(d: str, sid: str):
    """Backup session .jsonl before proactive reset (pre-compact guard)."""
    if not sid:
        return
    src = _transcript_path(d, sid)
    if not src.exists() or src.stat().st_size < BACKUP_MIN_BYTES:
        return
    try:
        ts  = time.strftime("%Y%m%d_%H%M%S")
        dst = BACKUP_DIR / f"{sid[:8]}_{ts}.jsonl"
        shutil.copy2(src, dst)
        backups = sorted(BACKUP_DIR.glob("*.jsonl"))
        for old in backups[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)
        log.info("[%s] session backup: %s -> %s", MACHINE, src.name, dst.name)
    except Exception as e:
        log.warning("[%s] session backup failed: %s", MACHINE, e)


# ── Process Management ────────────────────────────────────────────────────────

def kill_proc(proc):
    """Kill process tree (Windows: claude.cmd spawns node children)."""
    if not proc:
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def cancel_run(channel: str) -> bool:
    """Terminate running claude process for channel. Returns True if found."""
    proc = _active_proc.pop(channel, None)
    if proc is None:
        return False
    kill_proc(proc)
    log.info("[%s] cancel_run: killed ch=%s", MACHINE, channel)
    return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _touch_last_call():
    try:
        LAST_CALL_FILE.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    except Exception:
        pass


def _collect_outbox(outbox: str) -> list:
    p = Path(outbox)
    if not p.is_dir():
        return []
    return [str(f) for f in sorted(p.iterdir()) if f.is_file()]


_TOO_LONG_MARKERS = (
    "prompt is too long", "prompt too long", "input is too long",
    "context length", "context window", "context limit", "too many tokens",
)


def _looks_too_long(text: str) -> bool:
    if not text:
        return False
    return any(m in text.lower() for m in _TOO_LONG_MARKERS)


def _fmt_tool(block: dict) -> str:
    name = block.get("name", "tool")
    inp  = block.get("input") or {}
    if isinstance(inp, dict):
        detail = (inp.get("description") or inp.get("command") or
                  inp.get("file_path") or inp.get("path") or
                  inp.get("pattern") or inp.get("url") or "")
    else:
        detail = ""
    return (f"🔧 {name}: {str(detail)[:70]}")[:180]


def _aufg_suffix(channel: str):
    return channel.split("_", 1)[1] if "_" in channel else None


def _recent_context(d: str, channel: str, limit=30, maxlen=6000,
                    aufgabe_filter=None) -> str:
    f = os.path.join(d, "logs", "dialog.jsonl")
    if not os.path.exists(f):
        return ""
    suf = aufgabe_filter if aufgabe_filter is not None else _aufg_suffix(channel)
    rows = []
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if (e.get("aufgabe_id") or None) != suf:
                    continue
                rows.append(e)
    except Exception:
        return ""
    parts = []
    for e in rows[-limit:]:
        ts = (e.get("ts") or "")[:10]
        q  = " ".join((e.get("in")  or "").split())
        a  = " ".join((e.get("out") or "").split())
        if q or a:
            parts.append(f"[{ts}] F: {q[:130]} | A: {a[:130]}")
    return "\n".join(parts)[:maxlen]


def _reseed_prefix(d: str, channel: str, aufgabe_filter=None, root_id=None) -> str:
    ctx = _recent_context(d, channel, aufgabe_filter=aufgabe_filter)
    if not ctx:
        return ("[System: Die vorherige Session wurde wegen Laenge zurueckgesetzt. "
                "Kein Kurzprotokoll verfuegbar — frage bei Bedarf nach Kontext.]")
    return (
        "[System: Die vorherige Session wurde wegen Laenge zurueckgesetzt "
        "(Kontext-Limit). Letzte Punkte aus dem Gespraech als Kurz-Kontext:\n"
        + ctx
        + "\nKnuepfe daran an; frage nach falls Details fehlen.]"
    )


def _new_session_prefix(d: str, channel: str, aufgabe_filter=None) -> str:
    ctx = _recent_context(d, channel, aufgabe_filter=aufgabe_filter)
    if not ctx:
        return ""
    return (
        "[System: Neue Session (Gedaechtnis-Kontext aus Verlauf):\n"
        + ctx
        + "\nKnuepfe daran an falls relevant; frage nach falls Details fehlen.]"
    )


def _ensure_mcp_empty(d: str) -> str:
    mcp_file = os.path.join(d, ".mcp_empty.json")
    try:
        if not os.path.exists(mcp_file):
            with open(mcp_file, "w", encoding="utf-8") as fh:
                fh.write('{"mcpServers":{}}')
    except Exception:
        pass
    return mcp_file


# ── Single Run ────────────────────────────────────────────────────────────────

def _run_once(channel: str, d: str, prompt: str, session_id,
              inbox_dir: str, outbox_dir: str, extra_append: str = "",
              on_progress=None, on_start=None, on_session=None) -> tuple:
    """Single streaming Claude run. Returns (reply_text, new_session_id)."""
    _touch_last_call()
    mcp_file   = _ensure_mcp_empty(d)
    sys_prompt = _build_system_prompt(channel, d, inbox_dir, outbox_dir, extra_append)

    cmd = [
        CLAUDE_CMD,
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json",
        "--verbose",
        "--model", CLAUDE_MODEL,
        "--append-system-prompt", sys_prompt,
        "--strict-mcp-config",
        "--mcp-config", mcp_file,
    ]
    if session_id:
        cmd += ["--resume", session_id]

    log.info("[%s] claude START ch=%s sid=%s cwd=%s",
             MACHINE, channel, (session_id or "new")[:8], d)

    proc = subprocess.Popen(
        cmd, cwd=d,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    _active_proc[channel] = proc

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except Exception:
        pass

    if on_start:
        try:
            on_start(proc)
        except Exception:
            pass

    # Silence watchdog — fires if Claude goes silent for SILENCE_TIMEOUT seconds
    _killed = [False]

    def _do_kill():
        if not _killed[0]:
            _killed[0] = True
            kill_proc(proc)

    _stimer = [threading.Timer(SILENCE_TIMEOUT, _do_kill)]
    _stimer[0].daemon = True
    _stimer[0].start()
    _max_t = threading.Timer(MAX_TIMEOUT, _do_kill)
    _max_t.daemon = True
    _max_t.start()

    def _reset_silence():
        _stimer[0].cancel()
        t = threading.Timer(SILENCE_TIMEOUT, _do_kill)
        t.daemon = True
        _stimer[0] = t
        t.start()

    reply      = ""
    new_sid    = [session_id]
    _on_ses_cb = [on_session]

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            _reset_silence()
            try:
                ev = json.loads(line)
            except Exception:
                continue

            ev_type = ev.get("type", "")

            if ev_type == "system" and ev.get("session_id"):
                new_sid[0] = ev["session_id"]
                if _on_ses_cb[0]:
                    try:
                        _on_ses_cb[0](new_sid[0])
                    except Exception:
                        pass
                    _on_ses_cb[0] = None  # fire once

            elif ev_type == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    btype = block.get("type")
                    if btype == "tool_use" and on_progress:
                        try:
                            on_progress(_fmt_tool(block))
                        except Exception:
                            pass
                    elif btype == "text":
                        txt = (block.get("text") or "").strip()
                        if txt and on_progress:
                            try:
                                on_progress("💬 " + txt[:300])
                            except Exception:
                                pass

            elif ev_type == "result":
                reply = ev.get("result", "") or reply
                if ev.get("session_id"):
                    new_sid[0] = ev["session_id"]

    finally:
        _stimer[0].cancel()
        _max_t.cancel()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        _active_proc.pop(channel, None)

    if reply:
        log.info("[%s] claude DONE ch=%s len=%d sid=%s",
                 MACHINE, channel, len(reply), (new_sid[0] or "")[:8])
    else:
        log.warning("[%s] claude LEER ch=%s — kein Text vom CLI", MACHINE, channel)

    return (reply, new_sid[0])


# ── Public API ────────────────────────────────────────────────────────────────

def run_stream(channel: str, user_text: str,
               incoming_files=None, on_progress=None, on_start=None, on_notice=None,
               extra_append: str = "", work_dir: str = None,
               inbox_dir: str = None, outbox_dir: str = None,
               aufgabe_filter=None, root_id=None, on_session=None):
    """
    3-layer resilience engine (bot-core v1.6, Windows-adapted).

    Layer 1 (proactive):  session transcript > CTX_LIMIT_BYTES
                          -> backup + fresh session + reseed
    Layer 2 (emergency):  reply contains 'prompt too long'
                          -> backup + reset + retry
    Layer 3 (empty):      no reply at all
                          -> reset + summary request

    Returns (reply_text, outbox_files).
    """
    d       = work_dir or dir_for(channel)
    _inbox  = inbox_dir  or os.path.join(d, "_inbox")
    _outbox = outbox_dir or os.path.join(d, "_outbox")

    os.makedirs(_inbox,  exist_ok=True)
    os.makedirs(_outbox, exist_ok=True)
    os.makedirs(d,       exist_ok=True)

    incoming_files = incoming_files or []
    prompt = (user_text or "").strip()
    if incoming_files:
        prompt += ("\n\n[System: Uploads in " + _inbox + ": "
                   + ", ".join(os.path.basename(p) for p in incoming_files) + "]")
    if not prompt:
        return ("", [])

    def _say(msg):
        for cb in (on_progress, on_notice):
            if cb:
                try:
                    cb(msg)
                except Exception:
                    pass

    t0             = time.time()
    sid            = _load_session(d, channel)
    resuming       = bool(sid)
    session_resets = 0

    # ── Layer 1: proactive reset when context file exceeds limit ──────────────
    sz = _session_size(d, sid) if sid else 0
    if sid and sz > CTX_LIMIT_BYTES:
        log.warning("[%s] Layer 1: ctx %d > %d bytes — fresh session ch=%s",
                    MACHINE, sz, CTX_LIMIT_BYTES, channel)
        _backup_session_jsonl(d, sid)
        _save_session(d, channel, None)
        _say("♻️ Kontext-Limit (proaktiv) — Session gesichert, frischer Start, Kurz-Kontext übernommen.")
        prompt = (_reseed_prefix(d, channel, aufgabe_filter=aufgabe_filter, root_id=root_id)
                  + "\n\n---\n\n" + prompt)
        sid      = None
        resuming = False
        session_resets += 1

    # ── Memory context for brand-new sessions ─────────────────────────────────
    if not sid:
        mem = _new_session_prefix(d, channel, aufgabe_filter=aufgabe_filter)
        if mem:
            prompt = mem + "\n\n---\n\n" + prompt

    reply, new_sid = _run_once(
        channel, d, prompt, sid,
        _inbox, _outbox, extra_append,
        on_progress=on_progress, on_start=on_start, on_session=on_session,
    )

    if new_sid:
        _save_session(d, channel, new_sid)

    # ── Layer 2: context overflow detected in reply ───────────────────────────
    if resuming and _looks_too_long(reply):
        log.warning("[%s] Layer 2: 'prompt too long' — reset + reseed + retry ch=%s",
                    MACHINE, channel)
        _backup_session_jsonl(d, new_sid or sid)
        _save_session(d, channel, None)
        _say("♻️ Kontext-Limit erreicht — Session zurückgesetzt, neuer Versuch.")
        retry_prompt = (_reseed_prefix(d, channel,
                                       aufgabe_filter=aufgabe_filter, root_id=root_id)
                        + "\n\n---\n\n" + prompt)
        reply, new_sid = _run_once(
            channel, d, retry_prompt, None,
            _inbox, _outbox, extra_append,
            on_progress=on_progress, on_start=on_start, on_session=on_session,
        )
        if new_sid:
            _save_session(d, channel, new_sid)
        session_resets += 1

    # ── Layer 3: empty reply ──────────────────────────────────────────────────
    if not reply:
        log.warning("[%s] Layer 3: empty reply — reset + summary ch=%s", MACHINE, channel)
        _save_session(d, channel, None)
        session_resets += 1
        reply, new_sid = _run_once(
            channel, d,
            "Fasse in 1-2 Saetzen auf Deutsch zusammen was du fuer den User getan hast. "
            "Falls du noch nichts getan hast, beantworte die letzte Anfrage kurz.",
            None,
            _inbox, _outbox, extra_append,
            on_progress=on_progress, on_start=on_start, on_session=on_session,
        )
        if new_sid:
            _save_session(d, channel, new_sid)

    dauer_total = round(time.time() - t0, 1)
    if not reply:
        log.error("[%s] KEIN TEXT nach %d Versuchen, %.1fs, ch=%s",
                  MACHINE, session_resets + 1, dauer_total, channel)

    run_stream.last_meta = {
        "dauer_s":        dauer_total,
        "session_resets": session_resets,
        "machine":        MACHINE,
        "session_id":     _load_session(d, channel),
    }

    outfiles = _collect_outbox(_outbox)
    final = (reply or
             "❌ Keine Antwort (Claude hat zweimal keinen Text geliefert — bitte neu versuchen).")
    return (final, outfiles)


def archive_sent(channel: str, pfade: list):
    """Move sent _outbox files to _sent archive (no delete)."""
    d    = dir_for(channel)
    sent = os.path.join(d, "_sent")
    os.makedirs(sent, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for p in pfade:
        if os.path.isfile(p):
            try:
                shutil.move(p, os.path.join(sent, f"{stamp}_{os.path.basename(p)}"))
            except Exception:
                pass
