# WP: Infrastruktur-Robustheit (Rehearsal von villa131)

**Datum:** 2026-06-13  
**Quelle:** villa131-Bot — dort entwickelt und getestet, jetzt auf alle Bots übertragen  
**Status:** offen

## Hintergrund

Im villa131-Bot wurden heute vier kritische Robustheits-Fixes entwickelt und deployed.
Dieser WP beschreibt dieselben Änderungen für den Demobot (demobot_mm.py).

Der demobot ist der Ursprung der Architektur — daher **defensiv abgleichen**:
vor jedem Fix prüfen ob er nicht schon vorhanden ist (Code lesen, nicht blind patchen).

---

## Fix 1 — `_STARTUP_TS` Dedup (Doppel-Verarbeitung nach Restart)

**Problem:** Nach einem Restart verarbeitet der Bot Nachrichten nochmal die vor dem Restart
eingegangen sind → gleiche Nachricht erhält zwei Antworten (unterschiedliche Task-IDs).

**Ursache:** Mattermost liefert beim WebSocket-Reconnect Events nach die schon verarbeitet wurden.

**Fix:**
```python
_STARTUP_TS = int(time.time() * 1000)

# In event_handler, nach BOT_USER_ID-Check:
if post.get("create_at", 0) < _STARTUP_TS:
    return  # Nachricht vor Restart ignorieren
```

**Prüfen:** Gibt es in demobot_mm.py bereits einen `_STARTUP_TS` oder ähnlichen Guard?

---

## Fix 2 — PID-Lock (verhindert doppelte Prozesse)

**Problem:** Wenn der Bot manuell gestartet und danach per systemd neugestartet wird,
laufen zwei Prozesse gleichzeitig. Jede Nachricht wird doppelt verarbeitet.

**Fix:** Beim Start PID-File schreiben, beim nächsten Start alten Prozess graceful beenden —
aber nur wenn `/proc/<pid>/cmdline` bestätigt dass es wirklich `demobot_mm` ist.

```python
_PID_FILE = "/tmp/demobot_mm.pid"  # oder lokaler Pfad je nach OS

def _acquire_pidlock():
    # Prüfe /proc/<old_pid>/cmdline → nur killen wenn es demobot_mm.py ist
    # SIGTERM + max 5s warten → kein SIGKILL
    ...

# main(): als erstes aufrufen
def main():
    _acquire_pidlock()
    ...
```

**Hinweis:** Demobot läuft auf Windows-Laptop → `/proc` existiert nicht.
Auf Windows: `psutil.Process(old_pid).cmdline()` verwenden oder weglassen (systemd-only Fix).

---

## Fix 3 — `_create_post` Retry + sichtbarer Fehler

**Problem:** Wenn Mattermost einen 500-Fehler zurückgibt (transient), stirbt der Bot still.
Kein Fehler im Kanal sichtbar, Bot wirkt "tot".

**Fix A — Retry mit Backoff:**
```python
def _create_post(msg, root_id=None):
    for attempt in range(3):
        try:
            payload = {"channel_id": MM_CHANNEL_ID, "message": msg}
            if root_id:
                payload["root_id"] = root_id
            return driver.posts.create_post(payload)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 401:
                raise  # MM-Token ungültig — nicht retrybar
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if root_id:
                # Fallback: ohne Thread-Kontext posten
                return driver.posts.create_post({"channel_id": MM_CHANNEL_ID, "message": msg})
            raise
```

**Fix B — `_run_task` sichtbarer Fehler:**
```python
def _run_task(post, sender):
    try:
        _handle_post(post, sender)
    except Exception as e:
        log.exception("Fehler bei der Verarbeitung")
        try:
            _create_post(f"❌ Interner Fehler: {e}")
        except Exception:
            log.exception("Konnte Fehlermeldung nicht posten")
```

---

## Fix 4 — MM Auth-Flow (Claude Token-Ablauf)

**Problem:** Claude OAuth-Token läuft ab → alle Anfragen bekommen 401 zurück →
Bot antwortet nicht mehr, keine Rückmeldung an User.

**Fix:** Bot erkennt 401 in Claude-Antwort → postet Login-URL in MM-Kanal →
User klickt auf Handy → gibt Code in Kanal ein → Bot schließt Auth-Flow ab.

**Details:** Siehe villa131/villa_mm.py: `_is_auth_ok()`, `_start_mm_auth()`,
`_complete_mm_auth()`, `_STARTUP_TS` für Code-Erkennung im event_handler.

**Hinweis:** Demobot läuft auf Windows-Laptop — claude CLI-Pfad anpassen (`claude.cmd` oder ähnlich).
Prüfen ob `claude auth login` auf Windows denselben stdout-Output produziert.

---

## Abarbeitungsreihenfolge

1. **Defensiv lesen:** demobot_mm.py komplett lesen, Fixes markieren die schon drin sind
2. Fix 1 (`_STARTUP_TS`) — einfach, sicher, kein Risiko
3. Fix 3 (`_create_post` Retry) — einfach, kein Risiko
4. Fix 2 (PID-Lock) — nur wenn Bot auf Linux/VPS läuft, sonst weglassen
5. Fix 4 (Auth-Flow) — komplex, Windows-Besonderheiten klären

**Kein Code anfassen ohne GO.**
