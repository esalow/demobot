# -*- coding: utf-8 -*-
import os, sys, json, requests
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TOKEN  = os.environ["MM_TOKEN"]
BASE   = f"{os.environ.get('MM_SCHEME','https')}://{os.environ['MM_URL']}/api/v4"
TEAM   = "uxr7k9d7z7rtudu4e8try9fnbr"
EIKE   = os.environ.get("MM_OWNER_USER_ID", "7r35n597p3rg3em96be67uocjo")
H      = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}

# 1. Kanal anlegen
body = {
    "team_id":      TEAM,
    "name":         "casa-melissa-villa-131",
    "display_name": "Casa Melissa / Villa 131",
    "purpose":      "Immobilie PS1v131 — Spanien",
    "type":         "P",   # P = privat (nur eingeladene Mitglieder)
}
r = requests.post(f"{BASE}/channels", json=body, headers=H)
if not r.ok and r.status_code != 400:
    print(f"Fehler beim Anlegen: {r.status_code} {r.text}")
    sys.exit(1)

if r.status_code == 400 and "exists" in r.text.lower():
    # bereits vorhanden — ID holen
    r2 = requests.get(f"{BASE}/teams/{TEAM}/channels/name/casa-melissa-villa-131", headers=H)
    channel_id = r2.json()["id"]
    print(f"Kanal existiert bereits: {channel_id}")
else:
    channel_id = r.json()["id"]
    print(f"Kanal angelegt: {channel_id}")

# 2. Eike als Mitglied hinzufügen
r2 = requests.post(f"{BASE}/channels/{channel_id}/members",
                   json={"user_id": EIKE}, headers=H)
if r2.ok:
    print(f"Eike ({EIKE}) hinzugefügt")
elif r2.status_code == 400:
    print(f"Eike bereits Mitglied")
else:
    print(f"Warnung Mitglied: {r2.status_code} {r2.text}")

print(f"\nChannel-ID: {channel_id}")
print("Tipp: In Mattermost Sidebar die Kategorie 'Immobilien' oeffnen und Kanal reinziehen.")
