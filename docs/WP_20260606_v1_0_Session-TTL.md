# WP: Session-TTL + UTC-Timestamps + PreCompact-Hook bei Reset
**Stand:** 2026-06-06 | **Projekt:** demobot | **Status:** offen

## Kontext

Der demobot nutzt Claude-Sessions via `--resume`. Sessions wachsen unbegrenzt wenn
keine TTL gesetzt ist — bei vielen unrelated Tasks im Kanal entstehen 500 MB-Blobs
die bei jeder Nachricht vollständig an Claude gesendet werden. Außerdem sind alle
Timestamps aktuell in Lokalzeit (Europe/Berlin), was bei Remote-Nutzung aus anderen
Zeitzonen (Handy in Asien) zu inkonsistenten Logs führt.

## Gesammelte Punkte

1. **TTL-Reset** — Session löschen wenn letzte Aktivität > 2h (konfigurierbar via `DEMOBOT_SESSION_TTL`)
2. **Kurze Meldung bei Reset** — `🔄 Neue Session gestartet` im Chat (nicht silent)
3. **Projekt bleibt** — aktives Projekt (z.B. `umsatz_grizzly`) überlebt TTL-Reset, nur Session wird geleert
4. **PreCompact-Hook vor Reset** — Hook einmal manuell aufrufen mit der alten Session-ID bevor gelöscht wird → Session wird exportiert + in `sessions_index.db` indiziert (wie normale VSCode-Sessions)
5. **UTC überall** — alle Timestamps in `dialog.jsonl`, `_remote_log.jsonl` und `_bot_state.json` auf `datetime.now(timezone.utc)` umstellen
6. **TTL-Counter** — bei jeder Nachricht zurücksetzen (`last_activity` in `_bot_state.json`)

## Schritte

- [ ] `SESSION_TTL` Konstante aus Env `DEMOBOT_SESSION_TTL` (Default: 7200s = 2h)
- [ ] `_touch_activity()` — schreibt `last_activity` (UTC ISO) in `_bot_state.json`
- [ ] `_check_and_reset_ttl()` — prüft Delta, ruft PreCompact-Hook auf, löscht Session-ID, postet Meldung
- [ ] PreCompact-Hook-Aufruf: `precompact-hook.py` mit `{"session_id": "<id>"}` via stdin
- [ ] `_touch_activity()` am Ende jeder verarbeiteten Nachricht aufrufen
- [ ] `_check_and_reset_ttl()` am Anfang von `_handle_post` aufrufen (vor Claude-Start)
- [ ] Alle `datetime.datetime.now()` in `demobot_mm.py` → `datetime.datetime.now(datetime.timezone.utc)`
- [ ] Alle `datetime.datetime.now()` in `demobot_core.py` → UTC
- [ ] Bot neu starten + testen: Inaktivität simulieren, prüfen ob Reset + Meldung + Hook-Export

## Verifikation

- [ ] `_bot_state.json` enthält `last_activity` in UTC nach jeder Nachricht
- [ ] Nach simulierter Inaktivität (TTL auf 60s setzen zum Testen): Reset-Meldung im Kanal
- [ ] `~/.claude/hook_status.json` zeigt `precompact` wurde aufgerufen
- [ ] Exportierte Session-MD in `~/.claude/scripts/` vorhanden
- [ ] `dialog.jsonl` Timestamps sind UTC (enden auf `Z` oder `+00:00`)
