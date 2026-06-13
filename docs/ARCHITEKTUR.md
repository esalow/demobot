# Demobot — Architektur & Verzeichnisstruktur

**Erstellt:** 2026-06-12  
**Owner:** Eike Salow

---

## Überblick: Was läuft wo

```
┌─────────────────────────────────────────────┐
│  LAPTOP (PC-WLPT)                           │
│                                             │
│  demobot  → #demobot  (Kanal 1)             │
│  demobot2 → #demobot2 (Kanal 2)             │
│  demobot3 → #demobot3 (Kanal 3)             │
│                                             │
│  Claude CLI läuft lokal                     │
│  Token: ~/.claude/.credentials.json         │
│                                             │
│  Verbindung zum VPS: WSS Port 443           │
│  (outbound, kein Port-Forwarding nötig)     │
└──────────────┬──────────────────────────────┘
               │ WebSocket (WSS)
               │ REST API
               ▼
┌─────────────────────────────────────────────┐
│  VPS (Hetzner, hetzner-vps)                 │
│                                             │
│  Mattermost   → mm.salows.de               │
│  villa-manager → /opt/villa131/             │
│  priv-inventar → /opt/priv-inventar-bot/    │
│  fahrkartenbot → /opt/fahrkartenbot/        │
│                                             │
│  Claude CLI (VPS): /root/.claude/           │
│  Token: /root/.claude/.credentials.json     │
└─────────────────────────────────────────────┘
```

---

## Laptop: Verzeichnisstruktur

```
c:\projekte\demobot\                ← MASTER: Code + Instanz-1-State
│
├── demobot_mm.py                   ← Mattermost-Adapter
│     WebSocket-Loop, Debounce, Aufgaben-Routing,
│     Status-Posts, WS-Ping (25s), WS-Watchdog (90s)
│
├── demobot_core.py                 ← Claude-CLI-Engine
│     Session-Management, Streaming, Context-Limits,
│     Auto-Recover (♻️), Touch-Datei, Retry bei Leere
│
├── start_all.ps1                   ← Alle 3 Instanzen starten
├── refresh_claude_token.ps1        ← Token-Refresh (Task Scheduler)
│
├── .env                            ← Konfiguration
│     MM_URL, MM_TOKEN, MM_CHANNEL_ID_DEMOBOT,
│     DEMOBOT_CHANNEL_NAME, CLAUDE_CMD, DEMOBOT_TIMEOUT
│
├── .sessions.json                  ← Claude Session-IDs
│     {kanal_aufgabe: session_id}  → für --resume
│
├── _aufgaben.json                  ← Aufgaben-State
│     {id: {title, status, sub_seq, main_post_id, root_id}}
│
├── _bot_state.json                 ← Aktives Projekt/Vorgang
│     {name, type, dir, task_seq}
│
├── _inbox\                         ← User-Uploads (Bilder, PDFs, Audio)
├── _outbox\                        ← Bot-Outputs → werden auto in Chat gepostet
├── _sent\                          ← Archiv gesendeter Dateien
│
├── logs\
│   ├── bot.log                     ← stdout (meist leer)
│   ├── bot_err.log                 ← eigentliche Logs (Python logging → stderr)
│   ├── dialog.jsonl                ← vollständiger Dialog (In+Out, Timestamps)
│   └── autostart.log               ← DemobotWatchdog-Neustarts
│
├── docs\
│   ├── ARCHITEKTUR.md              ← diese Datei
│   ├── PARADIGMEN.md               ← Leitprinzipien
│   ├── WP_*.md                     ← Work Packages (abgeschlossen: _DONE)
│   ├── ANLEITUNG_Aufgaben.md       ← User-Anleitung Aufgaben-System
│   └── KONZEPT_Aufgaben_Nummerierung.md
│
└── villa\                          ← Villa-131-Kontext (lokal)
    ├── SPEC_concierge.md
    └── troubleshooting.md

c:\projekte\demobot2\               ← Instanz-2-State (KEIN CODE)
├── .env                            ← MM_CHANNEL_ID_DEMOBOT=demobot2-channel
├── .sessions.json
├── _aufgaben.json
├── _bot_state.json
└── _inbox\ _outbox\ _sent\ logs\

c:\projekte\demobot3\               ← Instanz-3-State (KEIN CODE)
└── (analog demobot2)
```

---

## VPS: Verzeichnisstruktur

```
/opt/villa131/                      ← Villa-Manager
├── villa_mm.py                     ← Mattermost-Adapter
├── CLAUDE.md                       ← Verhaltensregeln (autonom handeln)
├── fetch_gmail.py                  ← Gmail-Poller (cron alle 30 Min)
├── mietvertrag.py                  ← Mietvertrag-Generator
├── villa_db.sqlite                 ← Buchungsdatenbank
├── attachments/                    ← Anlagen zu Buchungen
├── logs/
│   └── fetch_gmail.log
└── venv/                           ← Python-Virtualenv

/opt/priv-inventar-bot/             ← Priv-Inventar
├── adapters/mm_adapter.py
├── teiledatenbank.db               ← SQLite
└── venv/

/opt/fahrkartenbot/                 ← Fahrkartenbot
└── bot.py

/root/.claude/
└── .credentials.json               ← OAuth-Token (geteilt von ALLEN VPS-Bots)

/root/refresh_claude_token.sh       ← Token-Refresh (cron alle 30 Min + Random)
/var/log/claude-token-refresh.log   ← Token-Refresh-Log
```

---

## Hintergrunddienste

### Laptop (Windows Task Scheduler)

| Task | Intervall | Was | Log |
|------|-----------|-----|-----|
| `DemobotWatchdog` | 5 Min | Prüft ob alle 3 Instanzen laufen, startet ggf. neu | `logs/autostart.log` |
| `ClaudeTokenRefresh` | 30 Min | Token frisch halten (skip wenn PC aktiv < 5 Min oder letzter Call < 25 Min) | `~/.claude/token_refresh.log` |
| `ClaudeJSONLBackup` | täglich 3h | Backup der Session-JSONL-Dateien | — |
| `Mailcenter-Pipeline-15min` | 15 Min | Mailcenter-Pipeline | Mailcenter-Logs |
| `VillaManager-AuthRefresh` | 25 Min | Villa-Auth aktuell halten | — |
| `QMD-DocIndex-Nightly` | täglich | Session-Gedächtnis indexieren | — |

### VPS (cron)

| Cron | Wann | Was | Log |
|------|------|-----|-----|
| `refresh_claude_token.sh` | */30 * * * * | VPS Claude-Token frisch halten | `/var/log/claude-token-refresh.log` |
| `villa fetch_gmail.py` | */30 * * * * | Gmail/Airbnb-Mails holen | `/opt/villa131/logs/fetch_gmail.log` |
| `daily_cleanup.py` | 0 3 * * * | Aufräumen | `/root/claude-meta-vps/cleanup.log` |
| `mssql backup.sh` | 30 2 * * * | MSSQL-Backup | `/var/log/mssql-backup.log` |

---

## Datenfluss: Nachricht → Antwort

```
User tippt in Mattermost (#demobot)
  │
  ▼
demobot_mm.py — event_handler()
  └── Debounce 1s (mehrere schnelle Nachrichten zusammenfassen)
        │
        ▼
  _flush_debounce()
  └── Aufgabe bestimmen (neuer Thread oder bestehend?)
  └── QUEUED-Post anlegen (Hauptkanal)
        │
        ▼
  _run_task() — Thread
  └── demobot_core.run_stream()
        ├── Session laden (--resume oder neu)
        ├── Context-Check (> 2MB → reset ♻️)
        ├── Claude CLI aufrufen (subprocess, stdin-Prompt)
        │     └── ~/.claude/last_claude_call.txt touch
        ├── Streaming lesen → on_progress → Live-Post editieren
        └── Ergebnis sammeln (result-Event)
              │
              ├── Leere Antwort? → Session reset + Retry
              └── Antwort vorhanden
                    │
                    ▼
        DONE-Post editieren (Hauptkanal, mit Vorschau)
        Thread-Antwort posten (#N.M)
```

---

## Verbindungen & Ports

| Von | Nach | Protokoll | Port | Zweck |
|-----|------|-----------|------|-------|
| Laptop | VPS (mm.salows.de) | WSS | 443 | Demobot WebSocket |
| Laptop | VPS | HTTPS | 443 | Mattermost REST API |
| Laptop | VPS | SSH | 22 | Headscale-Mesh (`hetzner-vps`) |
| VPS (villa) | Gmail | IMAP/HTTPS | 993/443 | Mail-Fetch |
| VPS | Mattermost (localhost) | HTTP | 8065 | villa/priv/fahrkarten-bot |

---

## Bekannte Schwachstellen (Stand 2026-06-12)

| # | Problem | Ursache | Fix |
|---|---------|---------|-----|
| 1 | Leere Claude-Antworten | Claude liefert nur Tool-Calls ohne Text | Session-Reset + Retry (Schicht 3) |
| 2 | WS-Zombie | NAT-Router droppt idle TCP | WS-Ping alle 25s ✅ |
| 3 | Token-Expiry | OAuth-Token läuft ab wenn keine Nutzung | Task Scheduler + Idle-Check ✅ |
| 4 | Kein Auto-Restart | Kein Service-Manager | DemobotWatchdog alle 5 Min ✅ |

---

## Changelog

| Datum | Was |
|-------|-----|
| 2026-06-07 | 3 Instanzen + start_all.ps1 eingeführt |
| 2026-06-09 | Aufgaben-Nummerierung (#N.M) + QUEUED/AKTUELL/DONE implementiert |
| 2026-06-11 | Kontext-Auto-Recover (♻️) bei > 2MB Session |
| 2026-06-12 | WS-Ping, DemobotWatchdog, Token-Idle-Check, Session-Reset-Retry |
| 2026-06-12 | ARCHITEKTUR.md + PARADIGMEN.md angelegt |
