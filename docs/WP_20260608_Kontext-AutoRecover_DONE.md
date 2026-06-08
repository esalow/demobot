# WP: Kontext-Auto-Recover (Session zu gross / "Prompt is too long")
**Stand:** 2026-06-08 | **Projekt:** demobot | **Status:** DONE

## Kontext / Problem

Der demobot fuehrt pro Aufgabe-Kanal eine Claude-CLI-Session via `--resume`. Bei
aktiver Nutzung wachsen diese Sessions, bis sie das Kontextfenster sprengen — die
CLI antwortet dann nur noch mit **"Prompt is too long"** und der Kanal haengt.
Ausgeloest u.a. dadurch, dass das 1M-Fenster zeitweise wegfiel (Fallback 200K).

Der bestehende **TTL-Reset** (`_check_and_reset_ttl`, siehe WP Session-TTL) greift
nur nach **Inaktivitaet** und nur fuer den **Haupt-Kanal** (`CHANNEL_NAME`). Eine
Session, die *waehrend aktiver Nutzung* zu gross wird, war damit nicht abgedeckt —
genau dieser Fall trat bei Kanal A1 auf (Transkript 5,4 MB).

Wichtig: Ein echtes `/compact` hilft hier NICHT — es muss den kompletten Verlauf
laden, um zu summieren, und laeuft auf einer bereits uebergrossen Session selbst in
"Prompt is too long". Loesung daher: **Reset + Kurz-Kontext (Reseed)**.

## Architektur (beim Fix dokumentiert)

- **3 Instanzen, EIN Code:** `start_all.ps1` startet `demobot`, `demobot2`,
  `demobot3` — alle mit demselben Script `c:\projekte\demobot\demobot_mm.py`, nur
  unterschiedliches Arbeitsverzeichnis (cwd) + eigene `.env`. demobot2/demobot3
  haben **keine eigenen `.py`-Dateien** — sie importieren `demobot_core.py` aus
  `c:\projekte\demobot`. **Folge: ein Fix in demobot/ gilt fuer alle drei.**
- Jede Instanz haengt an einem eigenen Mattermost-Kanal (eigene `MM_CHANNEL_ID`),
  identifiziert sich per `DEMOBOT_MACHINE` (PC-WLPT / laptop-db2 / laptop-db3 —
  Labels, faktisch alle auf PC-WLPT lauffaehig) und nutzt denselben Bot-Account.
- **Aufgaben/"Boxen" innerhalb eines Kanals** (A1, A2, …) sind virtuelle Threads
  mit je eigener Session `<channel>_A<id>`, gesteuert ueber EINEN globalen Zeiger
  `_cur_aufgabe[0]` in `demobot_mm.py`. (Siehe Offener Punkt.)

## Umsetzung (in `demobot_core.py`)

Zwei Schichten in `run_stream`, plus sichtbare Meldung:

1. **Schicht 1 (vorbeugend):** Vor jedem Lauf wird die Groesse des Session-
   Transkripts geprueft (`_session_size` -> `~/.claude/projects/<slug>/<sid>.jsonl`,
   slug via `_proj_slug`). Ueber `CTX_LIMIT_BYTES` (Env `DEMOBOT_CTX_LIMIT_BYTES`,
   Default 2 MB) -> Session auf `null`, Kurz-Kontext voranstellen, frisch starten.
2. **Schicht 2 (Notfall):** Liefert ein Resume-Lauf trotzdem "Prompt is too long"
   (`_looks_too_long`), wird die Session zurueckgesetzt, Kurz-Kontext vorangestellt
   und **einmal** neu versucht.
3. **Reseed:** `_recent_context` zieht die letzten ~12 Eintraege aus
   `logs/dialog.jsonl`, **gefiltert nach Aufgaben-ID** (`_aufg_suffix`), damit
   Threads nicht vermischt werden. `_reseed_prefix` baut den Kontext-Block.
4. **Sichtbare Meldung:** neuer Callback `on_notice` in `run_stream`; `demobot_mm.py`
   reicht ihn als permanente Kanal-Meldung durch (`_post_text`). Meldung:
   `♻️ Session war gross (X.X MB) — frisch gestartet mit Kurz-Kontext aus dem Verlauf.`

Geaenderte Dateien: `demobot_core.py` (Helfer + `_run_once` + Orchestrierung +
`on_notice`), `demobot_mm.py` (`on_notice`-Durchreichung an `run_stream`).

## Verifikation (erledigt)

- [x] `demobot_core.py` + `demobot_mm.py` kompilieren sauber.
- [x] `_session_size` liefert korrekt: A1 = 5,40 MB, A2 = 0,83 MB.
- [x] `_reseed_prefix("demobot_A1")` liefert nur A1-Inhalte (VPS/Hetzner-Faden).
- [x] Schicht 1 hat real gegriffen: A1-Session `bccc4f0a` (5,4 MB) -> neue Session
      `870c00ed`; Antwort #115 kam mit korrektem Kontext, kein "Prompt is too long".
- [x] Alle 3 Instanzen via `start_all.ps1` neu gestartet, alle "Websocket OK"
      (Kanaele demobot / demobot2 / demobot3).

## Tuning

- `DEMOBOT_CTX_LIMIT_BYTES` (Bytes, Default 2_000_000) — Schwelle Schicht 1.

## Offener Punkt (separat, eigenes WP -> WP_20260608_Parallele-Aufgaben)

Vom Owner spezifiziertes Zielbild (2026-06-08):

- **Pro Bot mehrere Aufgaben parallel** annehmen (A1, A2, A3 gleichzeitig).
- **Jede Aufgabe zeigt ihr Arbeitsverzeichnis** an, damit man auf dem Laptop
  gezielt in genau diesem Verzeichnis eine Session aufmachen kann.

Aktuelle Blocker im Code (Analyse):

- Ein einziger globaler `_cur_aufgabe[0]` -> normale Nachrichten gehen an EINE
  Aufgabe; A2/A3 nur per expliziter Adressierung ("A2 …") oder "zurueck".
- **Arbeitsverzeichnis ist global pro Instanz** (`_active_state["dir"]`):
  `_process` setzt `work_dir = _get_active()["dir"]` — alle Aufgaben einer
  Instanz arbeiten aktuell im SELBEN Verzeichnis. Pro-Aufgabe-Verzeichnis fehlt.
- Verzeichnis wird nicht pro Aufgabe angezeigt (nur globales Projekt-Label).

Technisch moeglich: Parallelitaet ist bereits angelegt (`threading` +
`_sem = Semaphore(AUFGABEN_MAX)` + pro-Aufgabe eigene Session `<channel>_A<id>`).
Was fehlt, ist pro-Aufgabe Verzeichnis/Projekt + sauberes Routing/Anzeige.
-> wird in eigenem WP umgesetzt, sobald Design bestaetigt.
