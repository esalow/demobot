# WP: Aufgaben-Nummerierung & Thread-Modell

**Version:** 1.0  
**Datum:** 2026-06-09  
**Status:** [x] implementiert  
**Ref:** KONZEPT_Aufgaben_Nummerierung.md, ANLEITUNG_Aufgaben.md

---

## Ziel

Jeder Thread = eine Aufgabe mit hierarchischer Nummerierung (`#13`, `#13.1`, `#13.2`).  
Hauptstrang zeigt 1 Post pro Aufgabe mit Status + Vorschau — wird editiert, nie neu gepostet.

---

## Ist-Zustand

- Jeder Claude-Call bekommt eine neue Nummer (`#280`, `#281`, ...)
- Pro Call wird ein neuer Post im Hauptstrang erstellt
- Status-Posts häufen sich im Hauptstrang auf

## Soll-Zustand

```
Hauptstrang:
  ▶️ #12 [IT-Konzepte]  AKTUELL — läuft #12.5
  🕐 #13 [claude-infra] QUEUED  — 3 Antworten
  ✅ #14 [QMD VPS]      DONE    — 2 Antworten
     ↳ "Playbook fertig. Bitte testen: ansible-playbook site.yml"

Thread #14:
  ✅ #14.1 — erste frage ...
  ✅ #14.2 — zweite frage ...
```

---

## Änderungen

### 1. Sub-Counter pro Aufgabe

**Datei:** `demobot_mm.py`

In `_aufgaben` Dict: neues Feld `sub_seq` (int, startet bei 0).

```python
_aufgaben[aid] = {
    ...
    "sub_seq": 0,          # NEU: zählt Bot-Antworten in diesem Thread
    "main_post_id": None,  # NEU: ID des Hauptstrang-Posts für diese Aufgabe
}
```

Hilfsfunktion:
```python
def _next_sub(aufgabe_id):
    with _aufgaben_lock:
        _aufgaben[aufgabe_id]["sub_seq"] += 1
        return _aufgaben[aufgabe_id]["sub_seq"]
```

### 2. Label-Format

**Datei:** `demobot_mm.py`

```python
# Statt: f"▶️ **#{tid}**{label}"
# Neu:
sub = _next_sub(aufgabe_id)
label_num = f"#{aufgabe_id}.{sub}" if aufgabe_id else f"#{tid}"
```

Anzeige im Thread: `▶️ **#13.2** [claude-infra] läuft … _frage_`  
Anzeige im Hauptstrang-Post: separat (siehe unten)

### 3. Hauptstrang-Post: 1 pro Aufgabe, editiert

**Datei:** `demobot_mm.py`

Neue Funktion `_update_aufgaben_post(aufgabe_id, status, preview=None)`:

```python
def _update_aufgaben_post(aufgabe_id, status, preview=None):
    """Legt Hauptstrang-Post an (beim ersten Mal) oder editiert ihn."""
    a = _get_aufgabe(aufgabe_id)
    if not a:
        return
    icons = {"QUEUED": "🕐", "AKTUELL": "▶️", "DONE": "✅", "FEHLER": "❌"}
    icon = icons.get(status, "•")
    proj = a.get("name", CHANNEL_NAME)
    sub = a.get("sub_seq", 0)
    antworten = f"{sub} Antwort{'en' if sub != 1 else ''}"
    laufend = f"läuft #{aufgabe_id}.{sub}" if status == "AKTUELL" else antworten
    text = f"{icon} **#{aufgabe_id}** [{proj}] **{status}** — {laufend}"
    if preview and status == "DONE":
        kurz = preview[:120].replace("\n", " ")
        text += f"\n   ↳ _{kurz}_"
    with _aufgaben_lock:
        post_id = _aufgaben.get(aufgabe_id, {}).get("main_post_id")
    if post_id:
        _patch(post_id, text)
    else:
        post = _create_post(text, MM_CHANNEL_ID)
        with _aufgaben_lock:
            if aufgabe_id in _aufgaben:
                _aufgaben[aufgabe_id]["main_post_id"] = post["id"]
        _save_aufgaben()
```

### 4. Status-Übergänge einbauen

In `_process()` an den richtigen Stellen aufrufen:

| Stelle | Aufruf |
|--------|--------|
| Vor `core.run_stream` (Call startet) | `_update_aufgaben_post(aid, "AKTUELL")` |
| Nach `core.run_stream` (Call fertig) | `_update_aufgaben_post(aid, "DONE", preview=reply)` |
| Wenn andere Aufgabe AKTUELL wird | `_update_aufgaben_post(aid, "QUEUED")` für alle wartenden |
| Bei Exception | `_update_aufgaben_post(aid, "FEHLER")` |

### 5. QUEUED setzen wenn Call eingeht aber belegt

In `_flush_debounce` / Thread-Start: wenn Semaphore voll oder andere Aufgabe läuft →
`_update_aufgaben_post(aid, "QUEUED")` bevor Thread startet.

### 6. Interner task_seq bleibt — wird nicht mehr in MM angezeigt

`_task_seq` und `tid` bleiben für Logging (`dialog.jsonl`) und `_tasks` Dict.  
Angezeigt in Mattermost wird nur noch `#aufgabe_id.sub_seq`.

### 7. Aufgaben ohne Thread (Hauptkanal direkt)

Wenn User im Hauptkanal schreibt (kein root_id) → `_open_aufgabe()` wie bisher,
`root_id` = ID des Bot-Antwort-Posts (erster Sub-Post = Thread-Opener).

---

## Nicht im Scope

- WS-Watchdog Fix (separates Problem, läuft parallel)
- VPS-Bots (villa-manager, priv-inventar-bot)
- demobot2/3 bekommen den Fix automatisch (gleicher Code)

---

## Verifikation

- [ ] Neue Nachricht im Hauptkanal → Aufgabe #N angelegt, Hauptstrang-Post erscheint
- [ ] Thread-Antwort → Post zeigt `#N.1`, `#N.2`, ...
- [ ] Hauptstrang-Post wechselt QUEUED → AKTUELL → DONE
- [ ] DONE-Post zeigt Vorschau der letzten Antwort
- [ ] Zweiter Thread öffnen während erster läuft → zweiter zeigt QUEUED
- [ ] Nach Neustart: `main_post_id` + `sub_seq` aus `_aufgaben.json` geladen
