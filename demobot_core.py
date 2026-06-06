# -*- coding: utf-8 -*-
"""
demobot_core.py — claude CLI Engine (frontend-agnostisch).

- Kanalname = Verzeichnis (Kanal 'demobot' -> C:\\projekte\\demobot)
- Session pro Channel (--resume) fuer Kontext
- Live-Streaming via --output-format stream-json -> on_progress je Schritt
- Prozess-Handle via on_start -> Adapter kann "stop" ausloesen
- Timeout-Watchdog killt haengende Laeufe
"""

import os
import json
import shutil
import socket
import datetime
import threading
import subprocess

_session_lock = threading.Lock()
MACHINE = os.environ.get("DEMOBOT_MACHINE") or socket.gethostname()

BASE_DIR = os.environ.get("DEMOBOT_BASE", r"C:\projekte")
CLAUDE_CMD = os.environ.get(
    "CLAUDE_CMD", r"C:\Users\Lenovo T460p\AppData\Roaming\npm\claude.cmd")
TIMEOUT = int(os.environ.get("DEMOBOT_TIMEOUT", "240"))
VORGANG_BASE = os.environ.get("DEMOBOT_VORGANG_BASE",
    r"G:\Meine Ablage\ESALOW-Archiv\_vorgaenge")
CLAUDE_META = os.environ.get("DEMOBOT_CLAUDE_META", r"C:\projekte\claude-meta")

APPEND_SYSTEM = (
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
    "WICHTIG: Keine blockierenden Wartezeiten. Antworte auf Deutsch mit echten Umlauten."
)


def dir_for(channel):
    return os.path.join(BASE_DIR, channel)


def _ensure(d):
    os.makedirs(os.path.join(d, "_inbox"), exist_ok=True)
    os.makedirs(os.path.join(d, "_outbox"), exist_ok=True)


def _session_file(d):
    return os.path.join(d, ".sessions.json")


def _load_session(d, channel):
    with _session_lock:
        f = _session_file(d)
        if os.path.exists(f):
            try:
                return json.load(open(f, encoding="utf-8")).get(channel)
            except Exception:
                return None
        return None


def _save_session(d, channel, sid):
    with _session_lock:
        f = _session_file(d)
        data = {}
        if os.path.exists(f):
            try:
                data = json.load(open(f, encoding="utf-8"))
            except Exception:
                data = {}
        data[channel] = sid
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)


def _build_cmd(channel, d, stream, extra_append="", inbox_dir=None, outbox_dir=None):
    """Baut die claude-Befehlszeile (OHNE Prompt — der kommt via stdin).

    WICHTIG: --append-system-prompt MUSS einzeilig sein. Ein Zeilenumbruch im
    Argument bricht den Windows-Batch-Wrapper claude.cmd -> claude faellt auf
    Plaintext zurueck und ignoriert --output-format stream-json. Darum hier alle
    Whitespace-/Newline-Folgen zu einzelnen Leerzeichen kollabieren.
    """
    _inbox = inbox_dir or os.path.join(d, "_inbox")
    _outbox = outbox_dir or os.path.join(d, "_outbox")
    _sources = os.path.join(CLAUDE_META, "sources.json")
    _registry = os.path.join(CLAUDE_META, "project_registry.md")
    append = (APPEND_SYSTEM.format(
                  work_dir=d, inbox_dir=_inbox, outbox_dir=_outbox,
                  sources_json=_sources, project_registry=_registry) +
              f"\n\nDu laeufst auf Maschine '{MACHINE}', Kanal '{channel}', "
              f"Arbeitsverzeichnis '{d}'. Wenn jemand fragt WER oder WO (welcher "
              f"Rechner) eine Aufgabe bearbeitet hat, nenne diese Maschine: '{MACHINE}'.")
    if extra_append:
        append += " " + extra_append
    append = " ".join(append.split())  # einzeilig erzwingen (sonst Plaintext-Bug)
    cmd = [CLAUDE_CMD,
           "--permission-mode", "dontAsk",
           "--append-system-prompt", append,
           "--strict-mcp-config"]
    cmd += (["--output-format", "stream-json", "--verbose"] if stream
            else ["--output-format", "json"])
    sid = _load_session(d, channel)
    if sid:
        cmd += ["--resume", sid]
    # --mcp-config braucht einen DATEI-PFAD. Inline-JSON wird auf Windows ueber den
    # claude.cmd-Wrapper entwertet (die Quotes gehen verloren -> claude sucht eine Datei
    # "{mcpServers:{}}" und bricht ab: "MCP config file not found"). Darum eine kleine
    # leere MCP-Config-Datei schreiben und ihren absoluten Pfad uebergeben.
    mcp_file = os.path.join(d, ".mcp_empty.json")
    try:
        if not os.path.exists(mcp_file):
            with open(mcp_file, "w", encoding="utf-8") as fh:
                fh.write('{"mcpServers":{}}')
    except Exception:
        pass
    cmd += ["--mcp-config", mcp_file]
    return cmd


def kill_proc(proc):
    """Prozessbaum killen (Windows: claude.cmd spawnt node-Kinder)."""
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


def _fmt_tool(b):
    name = b.get("name", "tool")
    inp = b.get("input", {}) or {}
    if inp.get("description"):
        detail = str(inp["description"])
    elif inp.get("command"):
        detail = str(inp["command"])
    elif inp.get("file_path"):
        detail = os.path.basename(str(inp["file_path"]))
    elif inp.get("path"):
        detail = os.path.basename(str(inp["path"]))
    elif inp:
        detail = str(next(iter(inp.values())))
    else:
        detail = ""
    return (f"🔧 {name}: {detail}")[:180]


def _collect_outbox(outbox):
    if not os.path.isdir(outbox):
        return []
    return [os.path.join(outbox, f) for f in sorted(os.listdir(outbox))
            if os.path.isfile(os.path.join(outbox, f))]


def run_stream(channel, user_text, incoming_files=None, on_progress=None, on_start=None,
               extra_append="", work_dir=None, inbox_dir=None, outbox_dir=None):
    """Fuehrt claude LIVE aus. on_progress(text) je Schritt, on_start(proc) sobald
    der Prozess laeuft (fuer stop). extra_append = zusaetzlicher System-Prompt-Hinweis.
    work_dir überschreibt das Arbeitsverzeichnis (für Projekt-Routing).
    inbox_dir/outbox_dir überschreiben die Datei-Pfade im System-Prompt.
    Gibt (reply_text, outbox_files) zurueck."""
    d = work_dir or dir_for(channel)
    if not os.path.isdir(d):
        return (f"Verzeichnis existiert nicht: {d}", [])
    _inbox = inbox_dir or os.path.join(d, "_inbox")
    _outbox = outbox_dir or os.path.join(d, "_outbox")
    os.makedirs(_inbox, exist_ok=True)
    os.makedirs(_outbox, exist_ok=True)
    incoming_files = incoming_files or []
    prompt = (user_text or "").strip()
    if incoming_files:
        prompt += ("\n\n[System: Uploads in " + _inbox + ": "
                   + ", ".join(os.path.basename(p) for p in incoming_files) + "]")
    if not prompt:
        return ("", [])

    cmd = _build_cmd(channel, d, stream=True, extra_append=extra_append,
                     inbox_dir=_inbox, outbox_dir=_outbox)
    # Prompt via stdin (NICHT -p): mehrzeilige Aufgaben bleiben erhalten und
    # umgehen den Batch-Wrapper-Bug (Newline im Argument -> Plaintext).
    proc = subprocess.Popen(cmd, cwd=d, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
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

    timer = threading.Timer(TIMEOUT, lambda: kill_proc(proc))
    timer.daemon = True
    timer.start()

    reply = ""
    new_sid = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = e.get("type")
            if t == "system" and e.get("session_id"):
                new_sid = e["session_id"]
            elif t == "assistant":
                for b in e.get("message", {}).get("content", []):
                    bt = b.get("type")
                    if bt == "tool_use" and on_progress:
                        try:
                            on_progress(_fmt_tool(b))
                        except Exception:
                            pass
                    elif bt == "text":
                        txt = (b.get("text") or "").strip()
                        if txt and on_progress:
                            try:
                                on_progress("💬 " + txt[:300])
                            except Exception:
                                pass
            elif t == "result":
                reply = e.get("result", "") or reply
                if e.get("session_id"):
                    new_sid = e["session_id"]
    finally:
        timer.cancel()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    if new_sid:
        _save_session(d, channel, new_sid)
    return (reply or "(fertig, keine Textantwort)", _collect_outbox(_outbox))


def archive_sent(channel, pfade):
    """Gesendete _outbox-Dateien ins Archiv _sent\\ verschieben (kein Loeschen)."""
    d = dir_for(channel)
    sent = os.path.join(d, "_sent")
    os.makedirs(sent, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for p in pfade:
        if os.path.isfile(p):
            try:
                shutil.move(p, os.path.join(sent, f"{stamp}_{os.path.basename(p)}"))
            except Exception:
                pass
