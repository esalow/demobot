#!/usr/bin/env python3
"""Patcht den JSON-Fallback-Block in run_stream.py auf dem VPS."""

path = "/opt/priv-inventar-bot/core/run_stream.py"

with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Block finden: von "except json.JSONDecodeError:" bis zum return (raw, ...)
start = end = None
for i, line in enumerate(lines):
    if "except json.JSONDecodeError:" in line:
        start = i
    if start is not None and "return (raw, [], [])" in line:
        end = i
        break

if start is None or end is None:
    print(f"FEHLER: Block nicht gefunden (start={start}, end={end})")
    raise SystemExit(1)

print(f"Block gefunden: Zeilen {start+1}–{end+1}")
print("Alter Block:")
for l in lines[start:end+1]:
    print(" ", repr(l))

# Einrückung des alten Blocks übernehmen
indent = "    "

# Neuer Block: sauber, ohne Shell-Escape-Probleme
new_block = [
    indent + "except json.JSONDecodeError:\n",
    indent + "    # Fallback: antwort-Feld per Regex retten\n",
    indent + '    _pat = r\'\"antwort\"\\s*:\\s*\"((?:[^\"\\\\\\\\]|\\\\\\\\\\.)*)\"\'\n',
    indent + "    _m = __import__('re').search(_pat, raw, __import__('re').DOTALL)\n",
    indent + "    if _m:\n",
    indent + "        _txt = _m.group(1)\n",
    indent + "        _txt = _txt.replace('\\\\n', '\\n').replace('\\\\t', '\\t')\n",
    indent + "        _txt = _txt.replace('\\\\\"', '\"').replace('\\\\\\\\', '\\\\')\n",
    indent + '        log.info("JSON-Parse fehlgeschlagen, Antwort per Regex extrahiert")\n',
    indent + "        return (_txt, [], [])\n",
    indent + '    log.warning("JSON-Parse fehlgeschlagen, Rohtext: %s", raw[:200])\n',
    indent + "    return (raw, [], [])\n",
]

lines[start:end+1] = new_block

with open(path, "w", encoding="utf-8") as f:
    f.writelines(lines)

print("OK: Patch geschrieben")
print("Neuer Block:")
for l in new_block:
    print(" ", repr(l))
