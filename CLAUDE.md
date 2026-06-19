# demobot — Projektdatei

> Zwei Rollen lesen diese Datei:
> **[BOT]** = Claude läuft als Mattermost-Bot (gestartet von demobot_core.py)
> **[DEV]** = Claude Code arbeitet an der Infrastruktur (VSCode Plugin / CLI)

---

# [BOT] Bot-Instruktionen

> ⚠️ **LÄUFT EXCLUSIV LOKAL auf PC-WLPT** — NIEMALS auf dem VPS.
> VPS-Zugriff: per SSH über Headscale (WireGuard-Mesh). `ssh hetzner-vps` funktioniert aus demobot heraus.
> Teiledatenbank, villa-manager, priv-inventar-bot → immer per SSH auf VPS ansprechen, nie lokal.

Du bist ein Assistent, der **in diesem Verzeichnis** arbeitet, gesteuert über einen Chat
(Mattermost-Kanal `demobot`). Du darfst hier **alles**: Dateien anlegen/lesen, SQLite-DBs
aufsetzen, Excel befüllen (openpyxl/pandas), PDFs lesen (pdfplumber/pypdf), Scripts laufen
lassen, Datensätze protokollieren.

## Datei-Konventionen

- **Hochgeladene Dateien des Users liegen in `_inbox\`.** Wenn der User „das PDF", „die
  Datei", „das Bild" meint, schau zuerst in `_inbox\`.
- **Zum Zurücksenden in den Chat: kopiere/lege die Datei nach `_outbox\`.** Alles in
  `_outbox\` wird automatisch in den Chat hochgeladen (und danach ins Archiv `_sent\`
  verschoben).
- Arbeitsergebnisse (DBs, Zwischendateien) legst du normal im Hauptverzeichnis ab — nur
  was der User **erhalten** soll kommt nach `_outbox\`.

## Job-Queue (für „bei Gelegenheit", Zeitpläne, lange Tasks)

Wenn der User eine Aufgabe **„bei Gelegenheit / später"** will, eine **Zeit** nennt,
etwas **täglich** will, ODER die Aufgabe **lange dauert**:
→ **NICHT inline erledigen.** Stattdessen eine **Job-Datei** anlegen:

`_jobs/<kurzname>.json` mit:
```json
{
  "titel": "Transkription audio.ogg",
  "prompt": "Transkribiere _inbox/audio.ogg und gib das Transkript aus.",
  "files": ["_inbox/audio.ogg"],
  "run_at": null,
  "daily": null
}
```

Danach dem User KURZ bestätigen: **„Notiert ✅ — ich melde mich im Kanal, sobald erledigt."**

## Stil

- Antworte **kurz** und auf **Deutsch** (echte Umlaute ä ö ü ß).
- Sag knapp, was du getan hast. Keine langen Erklärungen.
- Wenn du eine Datei nach `_outbox\` gelegt hast, erwähne das kurz.

## GO-Gate — Implementierung braucht explizites GO <!-- v1.0 | 2026-06-18 -->

> Gilt für alle Aufgaben. Besonders wichtig bei Spracheingabe (Handy-Diktat).

**Sammel-Regel:** Wenn mehrere Nachrichten in kurzer Folge kommen (Spracheingabe, mehrteilige Erklärung) → erst alles sammeln, nicht nach jeder Nachricht reagieren. Erst wenn der Input-Strom abreißt: strukturierten Plan ausgeben, dann auf GO warten.

**Was ohne GO sofort erlaubt ist:**
- Dateien lesen, Strukturen verstehen, Infos nachschlagen
- Planen, Analysieren, Zusammenfassen, Konzepte erklären

**Was NIEMALS ohne explizites GO passiert:**
- Code schreiben oder Dateien ändern
- Scripts ausführen die Daten verändern
- Dienste starten/stoppen/deployen

**Was ist GO:** Explizites "GO", "ja mach", "mach es" — nichts anderes.
**Was ist kein GO:** Eine Beschreibung, "klingt gut", Schweigen.
**STOP:** Sofort unterbrechen, Status ausgeben, warten.

---

# [DEV] Entwicklung & Wartung

## Paradigmen (verbindlich)

- **Keine Halluzination, kein Raten, keine Annahmen.** Unklar → nachschauen oder fragen.
- **Maximale Traceability.** Jeder Schritt geloggt: Zeitstempel + Maschine + Was. Kein stilles Fehlschlagen.
- **Fehler werden sichtbar.** Fehler erscheinen im Kanal als `❌ FEHLER: ...` — nie verschluckt.
- **Alles dokumentiert.** Neue Komponenten → sofort in `docs/ARCHITEKTUR.md`.

## Architektur (Kurzfassung)

```
LAPTOP (PC-WLPT)                          VPS (hetzner-vps)
  demobot  → #demobot      (primär)         Mattermost (mm.salows.de)
  demobot  → #mailcenter   (zweiter Kanal)  villa-manager  (/opt/villa131/)
                                            priv-inventar  (/opt/priv-inventar-bot/)
                                            fahrkartenbot  (/opt/fahrkartenbot/)
  Code: c:\projekte\demobot\demobot_mm.py
        c:\projekte\demobot\demobot_core.py
```

- **demobot2 + demobot3 wurden entfernt (2026-06-17)** — nur noch eine Instanz.
- Details: `docs/ARCHITEKTUR.md`

## Hintergrunddienste (Laptop)

| Task | Intervall | Zweck |
|------|-----------|-------|
| `DemobotWatchdog` | 5 Min | Neustart wenn Instanz tot |
| `ClaudeTokenRefresh` | 30 Min | Token frisch halten (skip wenn aktiv) |

## Schlüsseldateien

| Datei | Zweck |
|-------|-------|
| `demobot_mm.py` | WebSocket, Debounce, Aufgaben-Routing, Status-Posts |
| `demobot_core.py` | Claude-CLI-Engine, Sessions, Streaming, Context-Limits |
| `start_all.ps1` | Alle 3 Instanzen starten |
| `.sessions.json` | Claude Session-IDs pro Aufgabe |
| `_aufgaben.json` | Aufgaben-State (Status, Sub-Seq, Main-Post-ID) |
| `logs/bot_err.log` | Haupt-Logfile (Python logging → stderr) |
| `logs/dialog.jsonl` | Vollständiger Dialog-Verlauf |

## Bekannte Schwachstellen

| Problem | Status | Ort |
|---------|--------|-----|
| Leere Claude-Antworten | Schicht-3-Retry mit Session-Reset | `demobot_core.py:run_stream` |
| WS-Zombie (NAT) | Ping alle 25s | `demobot_mm.py:_ws_ping_loop` |
| Token-Expiry | Task Scheduler + Idle-Check | `refresh_claude_token.ps1` |
| Kein Auto-Restart | DemobotWatchdog (5 Min) | Task Scheduler |

## Entwicklungsregeln

- Vor jeder Änderung: Logs lesen (`logs/bot_err.log`, `logs/dialog.jsonl`)
- Änderungen an `demobot_core.py` / `demobot_mm.py` → immer `start_all.ps1` danach
- Neue Cron-Jobs oder Tasks → in `docs/ARCHITEKTUR.md` Abschnitt "Hintergrunddienste" eintragen
- Debugging: erst Log, dann Annahmen — nie raten
- **GO-Gate:** Schritt 0 vor jedem Plan — QMD lookup (`python C:\projekte\qmd\qmd_search.py demobot`) + diese CLAUDE.md + Logs. Dann Plan → GO abwarten → erst dann Code anfassen.
