# -*- coding: utf-8 -*-
"""Diagnose: fuehrt den EXAKTEN claude-Befehl des Bots aus und zeigt die Rohausgabe.
Aufruf auf der jeweiligen Maschine:  python diag_claude.py
Zeigt: CLAUDE_CMD, Arbeitsverzeichnis, Argumente und die ersten Zeilen der Rohausgabe
von claude (stream-json ODER Fehlermeldung) + Exit-Code."""
import sys
import os
import subprocess

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import demobot_core as core

channel = os.environ.get("DEMOBOT_CHANNEL_NAME", "lippstadt")
d = core.dir_for(channel)
os.makedirs(d, exist_ok=True)
cmd = core._build_cmd(channel, d, stream=True)

print("CLAUDE_CMD:", core.CLAUDE_CMD)
print("CWD       :", d)
print("ARGS:")
for a in cmd:
    print("   ", (a[:90] + "…") if len(a) > 90 else a)

print("---- RAW OUTPUT (max 25 Zeilen) ----")
p = subprocess.Popen(cmd, cwd=d, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                     errors="replace")
try:
    out, _ = p.communicate(input="sag kurz hallo", timeout=180)
except Exception as e:
    out = f"(communicate-Fehler: {e})"
for i, ln in enumerate((out or "").splitlines()[:25]):
    print(f"{i}: {ln[:200]}")
print("EXIT", p.returncode)
