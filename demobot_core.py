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
import re
import json
import shutil
import socket
import time
import logging
import datetime
import threading
import subprocess

_session_lock = threading.Lock()
log = logging.getLogger("demobot-core")
MACHINE = os.environ.get("DEMOBOT_MACHINE") or socket.gethostname()

BASE_DIR = os.environ.get("DEMOBOT_BASE", r"C:\projekte")
CLAUDE_CMD = os.environ.get(
    "CLAUDE_CMD", r"C:\Users\Lenovo T460p\AppData\Roaming\npm\claude.cmd")
LAST_CALL_FILE = os.path.join(
    os.environ.get("USERPROFILE", r"C:\Users\Lenovo T460p"),
    ".claude", "last_claude_call.txt")
SILENCE_TIMEOUT = int(os.environ.get("DEMOBOT_SILENCE_TIMEOUT",
                      os.environ.get("DEMOBOT_TIMEOUT", "600")))
MAX_TIMEOUT     = int(os.environ.get("DEMOBOT_MAX_TIMEOUT", "3600"))
VORGANG_BASE = os.environ.get("DEMOBOT_VORGANG_BASE",
    r"G:\Meine Ablage\ESALOW-Archiv\_vorgaenge")
CLAUDE_META = os.environ.get("DEMOBOT_CLAUDE_META", r"C:\projekte\claude-meta")
# Ab dieser Transkript-Groesse (Bytes) wird die Session vorbeugend frisch gestartet
# (mit Kurz-Kontext aus dialog.jsonl), bevor sie ins Kontext-Limit laeuft. Tunebar.
CTX_LIMIT_BYTES = int(os.environ.get("DEMOBOT_CTX_LIMIT_BYTES", str(2_000_000)))

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
           "--permission-mode", "bypassPermissions",
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


def _proj_slug(d):
    """Claude legt Transkripte unter ~/.claude/projects/<slug>/<sid>.jsonl ab.
    slug = Pfad, alle Nicht-Alphanumerischen Zeichen -> '-' (z.B.
    'C:\\projekte\\demobot' -> 'C--projekte-demobot')."""
    return re.sub(r"[^a-zA-Z0-9]", "-", d)


def _transcript_path(d, sid):
    return os.path.join(os.path.expanduser("~"), ".claude", "projects",
                        _proj_slug(d), f"{sid}.jsonl")


def _session_size(d, sid):
    """Groesse des Session-Transkripts in Bytes (0 wenn nicht vorhanden)."""
    if not sid:
        return 0
    try:
        return os.path.getsize(_transcript_path(d, sid))
    except Exception:
        return 0


def _aufg_suffix(channel):
    """'demobot_A1' -> 'A1'; 'demobot' -> None. Zum Filtern von dialog.jsonl."""
    return channel.split("_", 1)[1] if "_" in channel else None


_TOO_LONG_MARKERS = ("prompt is too long", "prompt too long", "input is too long",
                     "context length", "context window", "context limit",
                     "too many tokens")


def _looks_too_long(reply):
    r = (reply or "").lower()
    return any(m in r for m in _TOO_LONG_MARKERS)


def _recent_context(d, channel, limit=30, maxlen=6000, aufgabe_filter=None):
    """Kurzer Gespraechs-Kontext aus logs/dialog.jsonl.
    aufgabe_filter: explizite Aufgaben-ID z.B. 'A39' (überstimmt _aufg_suffix).
    Gefiltert nach Aufgaben-ID des Kanals, damit Threads nicht vermischt werden."""
    f = os.path.join(d, "logs", "dialog.jsonl")
    if not os.path.exists(f):
        return ""
    suf = aufgabe_filter if aufgabe_filter is not None else _aufg_suffix(channel)
    rows = []
    try:
        with open(f, encoding="utf-8") as fh:
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
        ts = (e.get("ts") or "")[:10]  # nur Datum
        proj = e.get("projekt") or ""
        q = " ".join((e.get("in") or "").split())
        a = " ".join((e.get("out") or "").split())
        if not (q or a):
            continue
        proj_tag = f" [{proj}]" if proj and proj != channel.split("_")[0] else ""
        parts.append(f"[{ts}]{proj_tag} F: {q[:130]} | A: {a[:130]}")
    return "\n".join(parts)[:maxlen]


_SUMMARIES_DIR = "_summaries"


def _summary_path(d, root_id):
    return os.path.join(d, _SUMMARIES_DIR, f"mm_{root_id}.md")


def _load_summary(d, root_id):
    if not root_id:
        return ""
    try:
        return open(_summary_path(d, root_id), encoding="utf-8").read().strip()
    except Exception:
        return ""


def _save_summary(d, root_id, text):
    if not root_id or not text:
        return
    os.makedirs(os.path.join(d, _SUMMARIES_DIR), exist_ok=True)
    with open(_summary_path(d, root_id), "w", encoding="utf-8") as fh:
        fh.write(text)


def _generate_summary(d, channel, sid):
    """Ruft Claude (bestehende Session) auf und bittet um Kontext-Zusammenfassung."""
    if not sid:
        return None
    mcp_file = os.path.join(d, ".mcp_empty.json")
    cmd = [CLAUDE_CMD,
           "--permission-mode", "bypassPermissions",
           "--output-format", "json",
           "--resume", sid,
           "--mcp-config", mcp_file,
           "--strict-mcp-config"]
    prompt = (
        "Erstelle eine kompakte Zusammenfassung dieser Konversation auf Deutsch. "
        "Format:\n## Thema\n[Was wird bearbeitet]\n\n## Stand\n[Was wurde entschieden/erreicht]\n\n"
        "## Offen\n[Was ist noch zu tun]\n\n## Key Facts\n[Wichtige Werte, Pfade, Namen]\n\n"
        "Maximal 2000 Zeichen. Nur Fakten, keine Prosa."
    )
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120, cwd=d)
        try:
            return json.loads(r.stdout).get("result", "").strip() or None
        except Exception:
            return None
    except Exception:
        log.warning("[%s] Summary-Generierung fehlgeschlagen kanal=%s", MACHINE, channel)
        return None


def _reseed_prefix(d, channel, aufgabe_filter=None, root_id=None):
    """Baut den Kurz-Kontext-Block, der nach einem Session-Reset vorangestellt wird.
    Wenn eine gespeicherte Zusammenfassung existiert, wird sie bevorzugt injiziert."""
    summary = _load_summary(d, root_id) if root_id else ""
    ctx = _recent_context(d, channel, aufgabe_filter=aufgabe_filter)
    if summary:
        block = "[System: Session zurückgesetzt (Größenlimit). Kontext-Zusammenfassung:\n" + summary
        if ctx:
            block += "\n\nLetzter Dialog:\n" + ctx
        return block + "]"
    if not ctx:
        return ("[System: Die vorherige Session wurde wegen Laenge zurueckgesetzt. "
                "Kein Kurzprotokoll verfuegbar — frage bei Bedarf nach Kontext.]")
    return ("[System: Die vorherige Session wurde wegen Laenge zurueckgesetzt "
            "(Kontext-Limit). Letzte Punkte aus dem Gespraech als Kurz-Kontext:\n"
            + ctx +
            "\nKnuepfe daran an; frage nach falls Details fehlen.]")


def _new_session_prefix(d, channel, aufgabe_filter=None):
    """Kurz-Kontext fuer den Start einer neuen Session (z.B. nach Bot-Neustart).
    Injiziert die letzten Dialoge damit der Bot sich 'erinnert'."""
    ctx = _recent_context(d, channel, aufgabe_filter=aufgabe_filter)
    if not ctx:
        return ""
    return ("[System: Neue Session (Gedaechtnis-Kontext aus Verlauf):\n"
            + ctx +
            "\nKnuepfe daran an falls relevant; frage nach falls Details fehlen.]")


def _touch_last_call():
    try:
        with open(LAST_CALL_FILE, "w") as f:
            f.write(datetime.datetime.now().isoformat())
    except Exception:
        pass


def _run_once(channel, d, prompt, on_progress, on_start, extra_append,
              inbox_dir, outbox_dir):
    """Ein einzelner claude-Lauf (Streaming). Speichert die neue Session-ID.
    Gibt (reply_text, outbox_files) zurueck."""
    _touch_last_call()
    t0 = time.time()
    sid_before = _load_session(d, channel)
    log.info("[%s] claude-call START kanal=%s sid=%s", MACHINE, channel, sid_before[:8] if sid_before else "neu")
    cmd = _build_cmd(channel, d, stream=True, extra_append=extra_append,
                     inbox_dir=inbox_dir, outbox_dir=outbox_dir)
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

    # Silence-Watchdog: feuert nur wenn Claude X Sek lang gar nichts ausgibt
    # Max-Timer: absolute Obergrenze unabhaengig von Output
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

    reply = ""
    new_sid = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            _reset_silence()  # Claude lebt -- Silence-Watchdog zuruecksetzen
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
        _stimer[0].cancel()
        _max_t.cancel()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    dauer = round(time.time() - t0, 1)
    if new_sid:
        _save_session(d, channel, new_sid)
    if reply:
        log.info("[%s] claude-call DONE kanal=%s dauer=%.1fs reply_len=%d", MACHINE, channel, dauer, len(reply))
    else:
        log.warning("[%s] claude-call LEER kanal=%s dauer=%.1fs — kein Text vom CLI", MACHINE, channel, dauer)
    return (reply, _collect_outbox(outbox_dir))


def run_stream(channel, user_text, incoming_files=None, on_progress=None, on_start=None,
               on_notice=None, extra_append="", work_dir=None, inbox_dir=None, outbox_dir=None,
               aufgabe_filter=None, root_id=None):
    """Fuehrt claude LIVE aus. on_progress(text) je Schritt, on_start(proc) sobald
    der Prozess laeuft (fuer stop). extra_append = zusaetzlicher System-Prompt-Hinweis.
    work_dir überschreibt das Arbeitsverzeichnis (für Projekt-Routing).
    inbox_dir/outbox_dir überschreiben die Datei-Pfade im System-Prompt.
    aufgabe_filter: Aufgaben-ID für dialog.jsonl-Filterung (z.B. 'A39').
    root_id: Mattermost-Thread-Root-ID für Zusammenfassung & Reseed.

    Kontext-Management:
    - Schicht 1 (vorbeugend): ist das Session-Transkript > CTX_LIMIT_BYTES, wird vor
      dem Reset eine Zusammenfassung generiert (root_id nötig), dann frisch gestartet.
    - Schicht 2 (Notfall): meldet ein Resume-Lauf 'prompt too long', wird die Session
      zurueckgesetzt, ein Kurz-Kontext vorangestellt und EINMAL neu versucht.
    Gibt (reply_text, outbox_files) zurueck."""
    d = work_dir or dir_for(channel)
    if not os.path.isdir(d):
        return (f"Verzeichnis existiert nicht: {d}", [])
    _inbox = inbox_dir or os.path.join(d, "_inbox")
    _outbox = outbox_dir or os.path.join(d, "_outbox")
    os.makedirs(_inbox, exist_ok=True)
    os.makedirs(_outbox, exist_ok=True)
    os.makedirs(d, exist_ok=True)
    incoming_files = incoming_files or []
    prompt = (user_text or "").strip()
    if incoming_files:
        prompt += ("\n\n[System: Uploads in " + _inbox + ": "
                   + ", ".join(os.path.basename(p) for p in incoming_files) + "]")
    if not prompt:
        return ("", [])

    def _say(msg):
        # on_progress = transiente Live-Zeile, on_notice = permanente Kanal-Meldung
        for cb in (on_progress, on_notice):
            if cb:
                try:
                    cb(msg)
                except Exception:
                    pass

    t0_total = time.time()
    sid = _load_session(d, channel)
    resuming = bool(sid)
    session_resets = 0

    # Gedaechtnis-Kontext bei neuer Session (kein bestehender Resume): letzte Dialoge voranstellen
    if not sid:
        mem = _new_session_prefix(d, channel, aufgabe_filter=aufgabe_filter)
        if mem:
            prompt = mem + "\n\n---\n\n" + prompt

    # Schicht 1 (vorbeugend): zu grosses Transkript -> Zusammenfassung + frisch starten
    sz = _session_size(d, sid)
    if sid and sz > CTX_LIMIT_BYTES:
        log.warning("[%s] Schicht-1: Session zu gross (%.1f MB) — reset + reseed kanal=%s",
                    MACHINE, sz / 1_000_000, channel)
        # Zusammenfassung generieren BEVOR die Session gelöscht wird
        if root_id:
            _say("🔍 Erstelle Kontext-Zusammenfassung vor Session-Reset …")
            summary = _generate_summary(d, channel, sid)
            if summary:
                _save_summary(d, root_id, summary)
                log.info("[%s] Zusammenfassung gespeichert root=%s…", MACHINE, root_id[:8])
        _say(f"♻️ Session war gross ({sz/1_000_000:.1f} MB) — frisch gestartet "
             f"mit Kontext-Zusammenfassung.")
        _save_session(d, channel, None)
        prompt = _reseed_prefix(d, channel, aufgabe_filter=aufgabe_filter,
                                root_id=root_id) + "\n\n---\n\n" + prompt
        resuming = False
        session_resets += 1

    reply, outfiles = _run_once(channel, d, prompt, on_progress, on_start,
                                extra_append, _inbox, _outbox)

    # Schicht 2 (Notfall): Resume trotzdem ins Limit gelaufen -> Reset + Reseed + 1x Retry
    if resuming and _looks_too_long(reply):
        log.warning("[%s] Schicht-2: Kontext-Limit im Resume — reset + reseed + retry kanal=%s",
                    MACHINE, channel)
        _say("♻️ Kontext-Limit erreicht — Session zurueckgesetzt, Kurz-Kontext "
             "uebernommen, neuer Versuch.")
        _save_session(d, channel, None)
        retry_prompt = _reseed_prefix(d, channel, aufgabe_filter=aufgabe_filter,
                                      root_id=root_id) + "\n\n---\n\n" + prompt
        reply, outfiles = _run_once(channel, d, retry_prompt, on_progress, on_start,
                                    extra_append, _inbox, _outbox)
        session_resets += 1

    # Schicht 3: Keine Textantwort -> Session zuruecksetzen + frisch nachfragen
    if not reply:
        log.warning("[%s] Schicht-3: leere Antwort — session reset + text-retry kanal=%s",
                    MACHINE, channel)
        _save_session(d, channel, None)
        session_resets += 1
        reply2, extra_files = _run_once(
            channel, d,
            "Fasse in 1-2 Saetzen auf Deutsch zusammen was du fuer den User getan hast. "
            "Falls du noch nichts getan hast, beantworte die letzte Anfrage kurz.",
            on_progress, on_start, extra_append, _inbox, _outbox)
        reply = reply2
        outfiles = outfiles or extra_files

    dauer_total = round(time.time() - t0_total, 1)
    if not reply:
        log.error("[%s] KEIN TEXT nach %d Versuchen, %.1fs, kanal=%s",
                  MACHINE, session_resets + 1, dauer_total, channel)

    final = reply or "❌ Keine Antwort (Claude hat zweimal keinen Text geliefert — bitte neu versuchen)."
    # Metadaten als Attribut fuer den Caller (optional nutzbar)
    run_stream.last_meta = {
        "dauer_s": dauer_total,
        "session_resets": session_resets,
        "machine": MACHINE,
    }
    return (final, outfiles)


def archive_sent(channel, pfade):
    """Gesendete _outbox-Dateien ins Archiv _sent\\ verschieben (kein Loeschen)."""
    d = dir_for(channel)
    sent = os.path.join(d, "_sent")
    os.makedirs(sent, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    for p in pfade:
        if os.path.isfile(p):
            try:
                shutil.move(p, os.path.join(sent, f"{stamp}_{os.path.basename(p)}"))
            except Exception:
                pass
