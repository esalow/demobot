# WP: Parallele Aufgaben pro Bot (Threads + Persistenz + Verzeichnis)
**Stand:** 2026-06-08 | **Projekt:** demobot | **Status:** offen

## Problem / Regression

Multi-Task (A1/A2/A3) war angelegt (`_aufgaben`, `_cur_aufgabe`, REF-Routing,
Commit `fbd6853`), aber **es verschwindet bei jedem Neustart**:

- `_aufgaben = {}`, `_cur_aufgabe = [None]`, `_aufgabe_seq = [0]` leben **nur im
  RAM** und werden **nie gespeichert**. Jeder Restart (50-Min-Auto-Cron, Crashes,
  manuelle Restarts) setzt sie zurueck -> naechste Nachricht legt wieder A1 an.
- Beleg in `dialog.jsonl`: letzte 18 Nachrichten alle A1, sogar "mach eine weitere
  Aufgabe auf" (Task 127) blieb in A1.
- Zusatz: Arbeitsverzeichnis ist global pro Instanz (`_active_state["dir"]`), nicht
  pro Aufgabe. Umschalten nur per starrer Schluesselwoerter, nicht natuerlich.

## Zielbild (vom Owner bestaetigt 2026-06-08)

- Pro Bot mehrere Aufgaben **parallel** (A1/A2/A3 …) — Sessions sind bereits isoliert
  (`<channel>_A<id>`), Threads/Semaphore vorhanden.
- **Anzeige:** Mattermost-**Thread pro Aufgabe**. Thread-Kopf zeigt **A-Nummer +
  Arbeitsverzeichnis**. Antworten/Live-Updates des Bots laufen im jeweiligen Thread.
- **Routing: nur explizit** — Kontext ergibt sich aus (a) dem Thread, in dem der
  User schreibt, oder (b) Praefix "A2 …" im Hauptkanal. Keine KI-Zuordnung.
  Plain-Nachricht im Hauptkanal -> aktuelle Aufgabe; bei Unklarheit nachfragen.
- **Persistenz:** Aufgaben ueberleben Neustart.

## Umsetzung (demobot_mm.py)

### 1. Persistenz (Kern-Fix)
- Neue Datei `_aufgaben.json` (pro Instanz-Verzeichnis, in `.gitignore`).
- `_save_aufgaben()` / `_load_aufgaben()`; speichert `_aufgaben`, `_cur_aufgabe`,
  `_aufgabe_seq`. `_save` nach jeder Mutation, `_load` in `main()` vor Websocket.
- Aufgabe-Objekt erweitern: `root_id` (Thread), `dir`, `name` (Projekt), `status`.

### 2. Threads
- Posting-Helfer `_create_post`/`_post_text`/`_post_file` um `root_id` erweitern
  (Mattermost: Reply via `root_id` im Post-Body).
- `_open_aufgabe(title, dir, name)`: postet Thread-Kopf
  `📋 A{id} — {title}\nVerzeichnis: {dir}` -> dessen Post-`id` = `root_id` der Aufgabe.
- `_process`: nutzt **Aufgaben-`dir`** (nicht global), postet Live/Ergebnis mit
  `root_id` = Thread der Aufgabe; Label `[A{id} · {basename(dir)}]`.

### 3. Routing (nur explizit)
- `event_handler`: `root_id` des eingehenden Posts lesen. Gehoert er zu einer
  bekannten Aufgabe (`root_id`-Map) -> diese Aufgabe.
- Sonst Hauptkanal: "A{n} …" -> Aufgabe n; "neue/andere aufgabe …" -> neue Aufgabe
  (neuer Thread); plain -> `_cur_aufgabe`, bei None nachfragen/auto-A1.
- **Debounce pro (user_id, thread_root)** statt nur user_id — sonst verschmelzen
  Nachrichten aus verschiedenen Threads.

### 4. Verzeichnis pro Aufgabe
- Beim Anlegen Arbeitsverzeichnis festhalten (Default = aktives Projekt/Instanz-Dir).
- Im Thread-Kopf + in jeder Antwort sichtbar. `/projekt`/`/vorgang` wirkt auf die
  aktuelle Aufgabe (deren `dir`/`name`), nicht global.

## Schritte
- [ ] `_load_aufgaben`/`_save_aufgaben` + `_aufgaben.json` in `.gitignore`
- [ ] Aufgabe-Objekt: `root_id`, `dir`, `name`
- [ ] Posting-Helfer mit `root_id`
- [ ] `_open_aufgabe` postet Thread-Kopf + speichert `root_id`
- [ ] `_process` nutzt Aufgaben-`dir` + postet im Thread + Label inkl. Verzeichnis
- [ ] `event_handler`: Thread-basiertes Routing (root_id-Map) + explizit "A{n}"
- [ ] Debounce-Key (user_id, thread_root)
- [ ] Restart + Live-Test (siehe Verifikation)

## Verifikation
- [ ] Nach Restart sind A1/A2/A3 inkl. Thread + Verzeichnis noch da (`_aufgaben.json`)
- [ ] Schreiben in Thread A2 -> Antwort im Thread A2, Session `<channel>_A2`
- [ ] "A1 …" im Hauptkanal -> landet in A1-Thread
- [ ] Zwei Aufgaben gleichzeitig aktiv (parallele Verarbeitung, getrennte Kontexte)
- [ ] Thread-Kopf zeigt korrektes Arbeitsverzeichnis je Aufgabe
