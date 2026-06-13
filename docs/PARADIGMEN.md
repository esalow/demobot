# Demobot — Paradigmen & Leitprinzipien

**Erstellt:** 2026-06-12  
**Owner:** Eike Salow  
**Gilt für:** demobot, demobot2, demobot3, villa-manager (VPS)

---

## 1. Keine Halluzination — kein Raten — keine Annahmen

- Wenn etwas nicht bekannt ist → **nachfragen oder nachschlagen**, nicht erfinden
- Kein "wahrscheinlich liegt die Datei in..." — nachschauen und sicher sein
- Bei unklarem User-Input: **kurz klären**, nicht interpretieren und falsch handeln
- Entscheidungen die Daten verändern: **erst bestätigen lassen**

## 2. Maximale Traceability — alles mit Zeitstempel

Jeder relevante Schritt wird geloggt — wann, wo, was, von wem:

### Was muss immer geloggt werden:

| Ereignis | Wo geloggt | Format |
|----------|-----------|--------|
| Nachricht empfangen | `dialog.jsonl` | `{ts, sender, in, aufgabe_id}` |
| Claude-Aufruf Start | `bot_err.log` | `INFO [ts] Aufgabe A7 gestartet` |
| Claude-Aufruf Ende | `dialog.jsonl` | `{ts, out, status, dauer_s}` |
| Leere Antwort / Fehler | `dialog.jsonl` + `bot_err.log` | `ERROR [ts] Leere Antwort, Session reset` |
| WS-Reconnect | `bot_err.log` | `WARNING [ts] WS reconnect nach Xs Idle` |
| Session Reset (♻️) | `bot_err.log` | `WARNING [ts] Session A7 zurückgesetzt (sz=2.1MB)` |
| Token Refresh | `~/.claude/token_refresh.log` | `[ts] REFRESH / SKIP (Grund)` |
| Bot-Neustart | `logs/autostart.log` | `[ts] DemobotWatchdog: N Instanzen tot, Neustart` |

### Logging-Regeln:
- **Zeitstempel immer ISO-Format:** `2026-06-12T14:35:22`
- **Maschine immer mit:** `[PC-WLPT]` oder `[VPS]` Präfix
- **Kein stilles Fehlschlagen** — jeder ERROR geht in Log UND als Meldung in den Kanal
- VPS-Logs: `/opt/villa131/logs/`, nicht nach `/dev/null`

## 3. Fehler werden sichtbar gemacht

- Bot-Fehler → **erscheinen im Mattermost-Kanal** als `❌ FEHLER: ...`
- Nicht mehr: stille `(fertig, keine Textantwort)` — immer klarer Fehlertext
- Wenn Claude zweimal keine Antwort liefert → Meldung mit Diagnose-Hinweis

## 4. Alles dokumentiert

- Neue Komponente → in `ARCHITEKTUR.md` eintragen
- Neuer Cron/Task → in `ARCHITEKTUR.md` Abschnitt "Hintergrunddienste" eintragen
- Neues Paradigma → hier ergänzen
- Architektur-Änderungen → mit Datum und Grund kommentieren

## 5. Keine Zombie-Prozesse, kein stilles Absterben

- DemobotWatchdog (alle 5 Min) stellt sicher dass alle 3 Instanzen laufen
- WS-Ping (alle 25s) verhindert NAT-Zombie-Verbindungen
- Token-Refresh (alle 30-60 Min) verhindert Auth-Expiry

---

## Changelog

| Datum | Wer | Was |
|-------|-----|-----|
| 2026-06-12 | Eike+Claude | Initial — nach Woche instabiler Bots |
