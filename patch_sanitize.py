#!/usr/bin/env python3
"""Ersetzt den JSON-Parse-Block in run_stream.py durch robusten Zeichen-Parser."""
import ast

PATH = "/opt/priv-inventar-bot/core/run_stream.py"

# Zuerst Backup wiederherstellen (vom heutigen Backup)
import glob, os
backups = sorted(glob.glob(PATH + ".bak_*"))
if backups:
    import shutil
    shutil.copy(backups[-1], PATH)
    print(f"Backup wiederhergestellt: {backups[-1]}")

with open(PATH, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Prompt-Patch
for i, line in enumerate(lines):
    if "ANTWORT: NUR valides JSON, kein Markdown drum herum:" in line:
        lines[i] = line.replace(
            "ANTWORT: NUR valides JSON, kein Markdown drum herum:",
            "ANTWORT: NUR valides JSON, kein Markdown drum herum. "
            "Zeilenumbrueche im \"antwort\"-Feld IMMER als \\n schreiben, "
            "NIE als echte Newlines:"
        )
        print(f"Prompt gepatcht (Zeile {i+1})")
        break

# Parse-Block finden: start = erste "    try:" nach Zeile 360, end = "Antwort nicht lesbar"
start = end = None
for i, line in enumerate(lines):
    if i > 360 and start is None and line.strip() == "try:":
        start = i
    if start is not None and "Antwort nicht lesbar" in line:
        end = i
        break

if start is None or end is None:
    print(f"Block nicht gefunden (start={start}, end={end})")
    exit(1)

print(f"Ersetze Zeilen {start+1}-{end+1}")

new_block = [
    "    def _sanitize_json(s):\n",
    "        # Escapet echte Newlines/Tabs innerhalb von JSON-Strings (Zeichen-Parser)\n",
    "        out, in_str, esc = [], False, False\n",
    "        for ch in s:\n",
    "            if esc:\n",
    "                out.append(ch); esc = False\n",
    "            elif ch == chr(92) and in_str:\n",
    "                out.append(ch); esc = True\n",
    "            elif ch == chr(34):\n",
    "                in_str = not in_str; out.append(ch)\n",
    "            elif in_str and ch == chr(10):\n",
    "                out.append(chr(92) + 'n')\n",
    "            elif in_str and ch == chr(13):\n",
    "                out.append(chr(92) + 'r')\n",
    "            elif in_str and ch == chr(9):\n",
    "                out.append(chr(92) + 't')\n",
    "            else:\n",
    "                out.append(ch)\n",
    "        return ''.join(out)\n",
    "\n",
    "    try:\n",
    "        data = json.loads(json_str)\n",
    "    except json.JSONDecodeError:\n",
    "        try:\n",
    "            data = json.loads(_sanitize_json(json_str))\n",
    "            log.info('JSON-Parse: sanitize erfolgreich')\n",
    "        except Exception:\n",
    "            log.warning('JSON-Parse fehlgeschlagen: %s', raw[:200])\n",
    "            return (raw.strip() or '(Antwort nicht lesbar)', [], [])\n",
]

new_lines = lines[:start] + new_block + lines[end + 1:]

with open(PATH, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

# Syntax-Check
with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()
try:
    ast.parse(src)
    print("Syntax OK — fertig.")
except SyntaxError as e:
    print(f"Syntax-Fehler: {e}")
