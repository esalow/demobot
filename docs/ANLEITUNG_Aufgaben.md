# Anleitung: Mit Aufgaben arbeiten im Demobot

## Kurzversion

1. Schreib eine Nachricht im Hauptstrang → öffnet Aufgabe
2. Bot antwortet im Thread
3. Sieh im Hauptstrang was zu tun ist
4. Antwort im Thread → Bot macht weiter

---

## So sieht der Hauptstrang aus

```
▶️ #12 [IT-Konzepte]  AKTUELL — läuft #12.5
🕐 #13 [claude-infra] QUEUED  — 3 Antworten
✅ #14 [QMD VPS]      DONE    — 2 Antworten
   ↳ "Bitte auf gpu-box testen: ansible-playbook site.yml"
```

| Status | Bedeutung | Was tun? |
|--------|-----------|----------|
| ▶️ AKTUELL | Bot arbeitet gerade | Abwarten |
| 🕐 QUEUED  | Bot hat's gesehen, kommt gleich | Abwarten |
| ✅ DONE    | Bot hat geantwortet | **Vorschau lesen → Thread öffnen → antworten/testen** |
| ❌ FEHLER  | Call fehlgeschlagen | Thread öffnen, nochmal versuchen |

---

## Nummerierung

- `#14` = Aufgabe 14 (Thread im Hauptstrang)
- `#14.1`, `#14.2`, `#14.3` = einzelne Bot-Antworten im Thread

Im Thread siehst du den Verlauf:
```
eike:     wie installiere ich headscale?
demo-bot: ✅ #14.1 — [Anleitung...]

eike:     und der Client?
demo-bot: ✅ #14.2 — [Client-Setup...]

eike:     läuft nicht, Fehler XY
demo-bot: ▶️ #14.3 läuft ...
```

---

## Mehrere Aufgaben gleichzeitig

Du kannst mehrere Threads gleichzeitig offen haben.
Bot arbeitet sie **der Reihe nach** ab (sequenziell).

```
Thread #12: du schreibst → QUEUED
Thread #13: läuft gerade → AKTUELL
Thread #14: du schreibst → QUEUED
```

→ #13 fertig → #12 wird AKTUELL → #14 wartet noch.

Für **echte Parallelität**: nutze #demobot2 und #demobot3.

---

## Tipps

- **Vorschau im Hauptstrang lesen** bevor du in den Thread gehst — spart Zeit
- **DONE heißt: du bist dran** — testen, antworten, nächsten Schritt machen
- **Lücken in Sub-Nummern** sind normal (abgebrochene Calls)
- **Neue Aufgabe** = neue Nachricht im Hauptstrang (nicht in einem bestehenden Thread)
