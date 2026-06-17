# Demobot — Handbuch

> Betrieb, Deployment und Self-Update für Windows-Instanzen.

## Architektur

```
Mattermost Channel (mm.salows.de)
    │
    │  WebSocket (HTTPS/443)
    ▼
demobot_mm.py          ← Mattermost-Adapter, läuft als Windows-Service via nssm
    │
    │  subprocess
    ▼
claude.exe             ← Claude CLI, nutzt Eikes Subscription
    │
    │  stdout (JSON-Events)
    ▼
demobot_core.py        ← Event-Parser, Session-Management, Datei-Routing
```

Der Bot läuft auf der **Zielmaschine** (z.B. Lippstadt-Server) und führt Aufgaben dort direkt aus.  
Alle Werkzeuge die Claude nutzt (Dateizugriff, SSH, Scripts) laufen lokal auf dem Server.

---

## Instanzen

| Maschine | Service-Name | MM-Channel | .env `DEMOBOT_MACHINE` |
|----------|-------------|------------|------------------------|
| Lippstadt-Server | `lippstadt-server-bot` | `lippstadt` | `lippstadt` |
| _(weitere)_ | `<name>-server-bot` | `<name>` | `<name>` |

Alle Instanzen teilen **dasselbe Git-Repo** (`github.com/esalow/demobot.git`).  
Die `.env` auf jeder Maschine bestimmt welcher Channel und welche Verzeichnisse genutzt werden.

---

## Windows-Service via nssm

Der Bot läuft als Windows-Service — startet beim Boot automatisch, überlebt Abstürze.

### Einmalig einrichten (neue Maschine)

```powershell
# nssm herunterladen nach C:\tools\nssm\
# dann:
$nssm = "C:\tools\nssm\win64\nssm.exe"
$py   = "C:\Users\grizzly\AppData\Local\Programs\Python\Python313\python.exe"
$dir  = "C:\projekte\lippstadt-server-bot"

& $nssm install lippstadt-server-bot $py "$dir\demobot_mm.py"
& $nssm set    lippstadt-server-bot AppDirectory $dir
& $nssm set    lippstadt-server-bot AppStdout    "$dir\bot.log"
& $nssm set    lippstadt-server-bot AppStderr    "$dir\bot_err.log"
& $nssm set    lippstadt-server-bot Start        SERVICE_AUTO_START
& $nssm start  lippstadt-server-bot
```

### Starten / Stoppen

```powershell
C:\tools\nssm\win64\nssm.exe start   lippstadt-server-bot
C:\tools\nssm\win64\nssm.exe stop    lippstadt-server-bot
C:\tools\nssm\win64\nssm.exe restart lippstadt-server-bot
C:\tools\nssm\win64\nssm.exe status  lippstadt-server-bot
```

---

## Self-Update — wie der Bot sich selbst neustartet

Das ist der kritische Teil: **der Bot muss sich selbst killen** ohne dabei die Update-Meldung zu verlieren.

### Ablauf `/deploy`

```
User schreibt: /deploy

1. Bot erkennt /deploy → startet _do_deploy() in eigenem Thread
2. Bot postet: "🔄 Prüfe auf Updates …"
3. _do_deploy() ruft git pull auf
   → "Already up to date" → fertig, kein Neustart
   → Änderungen da → postet: "✅ Update: ... 🔄 Neustart in 3s …"
4. time.sleep(3)                ← Mattermost-Post wird zugestellt
5. Popen([nssm, "restart", svc]) ← NON-BLOCKING! Bot startet nssm, wartet nicht.
6. nssm beendet den Python-Prozess (kill)
7. nssm startet neuen Python-Prozess mit neuem Code
8. Neuer Bot verbindet sich, postet: "Verbunden als @lippstadt-server-bot …"
```

### Warum `Popen` statt `run`?

```python
# FALSCH — Bot wartet auf nssm, nssm wartet auf Bot → Deadlock
subprocess.run([nssm, "restart", svc])

# RICHTIG — Bot gibt nssm den Auftrag und läuft weiter (3s bis kill)
subprocess.Popen([nssm, "restart", svc])
```

`Popen` ist nicht-blockierend. Der Bot-Prozess läuft noch ~200ms weiter,  
dann killt nssm ihn und startet den neuen Prozess.

### Was überlebt den Neustart?

- ✅ Alle Sessions (`_aufgaben.json`, `_bot_state.json`) — auf Disk gespeichert
- ✅ Mattermost-Token — in `.env`
- ✅ Claude-Kontext — Session-ID in JSON, `--resume` beim nächsten Start
- ❌ Laufende Aufgaben — werden abgebrochen (daher: /deploy nur wenn nichts läuft)

---

## Normaler Deploy-Prozess (wenn der Bot selbst nicht erreichbar ist)

```powershell
# deploy.ps1 auf dem Server ausführen:
cd C:\projekte\lippstadt-server-bot
.\deploy.ps1
```

Das Script macht:
1. `git pull` — neuesten Code holen
2. `.pyc` Cache löschen
3. `nssm stop` + alle Bot-Prozesse killen
4. `nssm start` — Bot neu starten
5. Log-Tail zeigen (Verbindung bestätigen)

---

## Chat-Befehle

| Befehl | Wirkung |
|--------|---------|
| `/deploy` oder `/update` | git pull + nssm restart (Self-Update) |
| `/projekt <name>` | Arbeitsverzeichnis wechseln |
| `/vorgang <name>` | Auf Vorgang wechseln |
| `/zurück` | Zurück zu Kanal-Standard |
| `was läuft` | Laufende Aufgaben anzeigen |
| `stop #3` | Aufgabe #3 abbrechen |
| `go` / `deckel drauf` | Von Dialog- in Ausführ-Modus wechseln |

---

## .env Konfiguration

```env
MM_URL=mm.salows.de
MM_SCHEME=https
MM_PORT=443
MM_TOKEN=<mattermost-bot-token>
MM_CHANNEL_ID_DEMOBOT=<channel-id>
MM_OWNER_USER_ID=<owner-user-id>
DEMOBOT_MACHINE=lippstadt          # Name dieser Instanz
DEMOBOT_CHANNEL_NAME=lippstadt     # MM-Channel den der Bot überwacht
DEMOBOT_BASE=C:\projekte           # Wurzel für Projektverzeichnisse
CLAUDE_CMD=C:\Users\grizzly\.local\bin\claude.exe
DEMOBOT_TIMEOUT=240                # Max Sekunden pro Claude-Aufruf
DEMOBOT_SERVICE_NAME=lippstadt-server-bot  # nssm Service-Name für /deploy
```

---

## Log-Dateien

| Datei | Inhalt |
|-------|--------|
| `bot.log` | Verbindungen, Aufgaben, Fehler |
| `bot_err.log` | stderr (sollte leer sein) |
| `C:\projekte\lippstadt\logs\dialog.jsonl` | Alle Dialoge (Eingang + Antwort) |

---

## Troubleshooting

**Bot antwortet nicht:**
1. `nssm status lippstadt-server-bot` → SERVICE_RUNNING?
2. `Get-Content bot.log -Tail 20` → Fehler sichtbar?
3. Claude-Auth ok? → Im Log "Claude-Auth ungültig" → Bot postet Login-URL im Channel

**Claude-Auth abgelaufen:**
Bot erkennt 401-Fehler selbst und postet einen Login-Link im Mattermost-Channel.  
Link öffnen, einloggen, Code in den Channel schreiben → Bot bestätigt Login.

**Disk voll → Bot stirbt:**
Claude-Config (`~/.claude.json`) und andere Dateien können beim Schreiben abbrechen.  
Disk frei machen, dann Config aus Backup wiederherstellen:
```powershell
copy "$env:USERPROFILE\.claude\backups\.claude.json.backup.*" "$env:USERPROFILE\.claude.json" /Y
```
