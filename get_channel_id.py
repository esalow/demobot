# -*- coding: utf-8 -*-
import sys, requests
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
TOKEN = "kfpwd948ybghbrrhcm94hsk7bc"  # @system-bot
H = {"Authorization": "Bearer " + TOKEN}
team = requests.get("https://mm.salows.de/api/v4/teams/name/eikes-welt", headers=H, timeout=20).json()
ch = requests.get(f"https://mm.salows.de/api/v4/teams/{team['id']}/channels/name/demobot", headers=H, timeout=20).json()
print("demobot channel id:", ch.get("id"), "| name:", ch.get("name"))
