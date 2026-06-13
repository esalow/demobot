# Konzept: Aufgaben-Nummerierung & Thread-Modell

Stand: 2026-06-09

## Grundprinzip

**Jeder Thread = eine Aufgabe.**

Der User öffnet eine Aufgabe indem er eine Nachricht im Hauptstrang schreibt.
Der Bot antwortet im Thread — nicht im Hauptstrang.
Der Hauptstrang zeigt nur den Status-Überblick.

---

## Hauptstrang (Übersicht)

Ein Post pro Aufgabe. Wird bei jeder Statusänderung editiert — kein neuer Post.

### Status-Labels

| Label | Icon | Bedeutung |
|-------|------|-----------|
| `QUEUED` | 🕐 | Nachricht wartet, ein anderer Call läuft gerade |
| `AKTUELL` | ▶️ | Claude arbeitet gerade an dieser Aufgabe |
| `DONE` | ✅ | Letzter Call fertig, keine offene Nachricht |
| `FEHLER` | ❌ | Letzter Call fehlgeschlagen |

### Beispiel-Verlauf

**Zustand A** — #12 ist aktiv, #13 und #14 warten:
```
▶️ #12 [IT-Konzepte]  AKTUELL — läuft #12.5
🕐 #13 [claude-infra] QUEUED  — 3 Antworten
🕐 #14 [QMD VPS]      QUEUED  — 1 Antwort
```

**Zustand B** — #12 fertig, #13 wird abgearbeitet:
```
✅ #12 [IT-Konzepte]  DONE    — 5 Antworten
▶️ #13 [claude-infra] AKTUELL — läuft #13.4
🕐 #14 [QMD VPS]      QUEUED  — 1 Antwort
```

**Zustand C** — alle fertig, mit letzter Antwort als Vorschau:
```
✅ #12 [IT-Konzepte]  DONE — 5 Antworten
   ↳ "headscale_mesh_architektur.md erstellt. MSSQL Docker läuft, gpu-box noch ausstehend."

✅ #13 [claude-infra] DONE — 4 Antworten
   ↳ "Ansible-Playbook für Node-Provisioning fertig, liegt in claude-infra/playbooks/."

✅ #14 [QMD VPS]      DONE — 2 Antworten
   ↳ "QMD-Service läuft auf Port 3838, nginx-Config aktualisiert."
```

### Wer setzt den Status?

**Immer der Bot** — nie der User.

| Wer | Aktion | → Status |
|-----|--------|----------|
| Bot | empfängt User-Nachricht, startet sofort | → AKTUELL |
| Bot | empfängt User-Nachricht, anderer Call läuft | → QUEUED |
| Bot | Call fertig, keine offene Nachricht mehr | → DONE |
| Bot | startet nächsten QUEUED-Call | → AKTUELL |
| Bot | Call fehlgeschlagen | → FEHLER |

### Was DONE bedeutet

**DONE = Ball liegt beim User.**

Bot hat geantwortet. Jetzt ist der User dran — testen, antworten, nächsten Schritt machen.
Die Vorschau zeigt die letzte Bot-Antwort → User sieht auf einen Blick was zu tun ist,
ohne den Thread öffnen zu müssen.

```
✅ #13 [claude-infra] DONE — 4 Antworten
   ↳ "Playbook fertig. Bitte auf gpu-box testen: ansible-playbook site.yml"
```
→ User weiß: muss testen, dann in Thread #13 zurückschreiben.

---

## Thread (Aufgabe)

Jede Bot-Antwort im Thread bekommt eine Sub-Nummer: `Aufgaben-ID.Sub-Counter`

```
eike:     erste frage
demo-bot: ✅ #14.1 — erste frage
          [Antwort-Text]

eike:     zweite frage
demo-bot: ▶️ #14.2 läuft ...
          → editiert zu:
          ✅ #14.2 — zweite frage
          [Antwort-Text]
```

Sub-Counter:
- Startet bei 1 für jede Aufgabe
- Zählt hoch bei jeder Bot-Antwort
- Lücken sind ok (abgebrochener Call → Nummer fehlt)
- Nicht fortlaufend über Aufgaben hinweg

---

## Nummerierung

| Was | Nummer | Beispiel |
|-----|--------|---------|
| Aufgabe (Thread) | `#N` | `#14` |
| Sub-Call im Thread | `#N.M` | `#14.1`, `#14.2` |
| Projekt-Kontext | `[Name]` | `[IT-Konzepte]`, `[claude-infra]` |

Aufgaben-Nummer = Aufgaben-Seq (`_aufgabe_seq`), nicht der interne Task-Counter.
Interner `_task_seq` wird nicht mehr angezeigt.

---

## Parallelarbeit

### Innerhalb eines Kanals (sequenziell)
Mehrere Threads können gleichzeitig offen sein.
Claude antwortet **sequenziell** — ein Call nach dem anderen.
Während #14.2 läuft, wartet eine neue Nachricht in Thread #12.

```
Thread #12:  ✅ #12.1  ✅ #12.2  [wartet auf #12.3]
Thread #14:  ✅ #14.1  ▶️ #14.2 läuft
```

### Echte Parallelität (3 Kanäle)
```
#demobot   → Aufgabe über IT-Konzepte
#demobot2  → Aufgabe über claude-infra     } alle gleichzeitig
#demobot3  → Aufgabe über villa-manager   }
```
Jeder Kanal = eigene Bot-Instanz = eigener Claude-Prozess = wirklich parallel.

---

## Noch offen (für WP)

- [ ] Sub-Counter pro Aufgabe implementieren (`sub_seq` in `_aufgaben`)
- [ ] Hauptstrang-Post: 1 Post anlegen + editieren statt neuen posten
- [ ] Anzeige-Format: `#N.M` statt `#280`
- [ ] Interner `_task_seq` bleibt für Logging, wird nicht mehr in MM angezeigt
