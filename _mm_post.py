# -*- coding: utf-8 -*-
"""
_mm_post.py — UTF-8-sicherer Mattermost-Post-Helper fuer Claude Code (Windows).

Problem: Windows-Shell (cp1252) beschaedigt Umlaute wenn Text inline uebergeben wird.
Loesung: Nachricht in Datei schreiben (Write-Tool = immer UTF-8), dann dieses Script.

Verwendung:
    python _mm_post.py <kanal> <nachrichten_datei>
    python _mm_post.py <kanal> -         (liest von stdin)

Kanal-Aliases:
    demobot         -> kxnea5j5dfrj5nzgqdwauqhbpa
    teiledatenbank  -> mk4isj3ykbnmmqzpq91ppxpifh
    (oder direkt eine Channel-ID angeben)

Beispiel-Workflow fuer Claude Code:
    1. Write-Tool: Nachricht nach _msg.txt (UTF-8)
    2. Bash: python _mm_post.py teiledatenbank _msg.txt
"""

import sys
import os
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MM_URL    = os.environ["MM_URL"]
MM_TOKEN  = os.environ["MM_TOKEN"]
MM_SCHEME = os.environ.get("MM_SCHEME", "https")
MM_PORT   = int(os.environ.get("MM_PORT", "443"))

CHANNEL_ALIASES = {
    "demobot":        "kxnea5j5dfrj5nzgqdwauqhbpa",
    "teiledatenbank": "mk4isj3ykbnmmqzpq91ppxpifh",
    "villa131":       "hotk9qocx7rqt8gk95dfmcuc1c",
    "casa-melissa":   "hotk9qocx7rqt8gk95dfmcuc1c",
}

def main():
    if len(sys.argv) < 3:
        print("Verwendung: python _mm_post.py <kanal> <datei|->\n"
              "Kanaele: demobot, teiledatenbank (oder direkte Channel-ID)")
        sys.exit(1)

    kanal_arg = sys.argv[1]
    channel_id = CHANNEL_ALIASES.get(kanal_arg, kanal_arg)

    datei_arg = sys.argv[2]
    if datei_arg == "-":
        text = sys.stdin.read()
    else:
        with open(datei_arg, encoding="utf-8") as f:
            text = f.read()

    text = text.strip()
    if not text:
        print("Fehler: leere Nachricht")
        sys.exit(1)

    api = f"{MM_SCHEME}://{MM_URL}/api/v4"
    headers = {"Authorization": "Bearer " + MM_TOKEN, "Content-Type": "application/json"}

    # Lange Nachrichten aufteilen (MM-Limit: 16384 Zeichen)
    chunks = [text[i:i+16000] for i in range(0, len(text), 16000)]
    for chunk in chunks:
        r = requests.post(f"{api}/posts",
                          json={"channel_id": channel_id, "message": chunk},
                          headers=headers, timeout=30)
        if not r.ok:
            print(f"Fehler {r.status_code}: {r.text}")
            sys.exit(1)

    print(f"OK — {len(chunks)} Post(s) an #{kanal_arg} gesendet")

if __name__ == "__main__":
    main()
