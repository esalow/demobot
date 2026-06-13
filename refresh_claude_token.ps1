# refresh_claude_token.ps1 — haelt den Claude-Token auf dem Laptop frisch.
# Wird vom Windows Task Scheduler alle 30 Min aufgerufen.
# Zufaellige Verzoegerung 0-30 Min -> effektives Intervall 30-60 Min.
# Skip-Logik: Wenn Claude kuerzlich genutzt wurde (Touch-Datei < 25 Min)
#             oder PC idle < 5 Min -> nicht stören.

$LOG         = "C:\Users\Lenovo T460p\.claude\token_refresh.log"
$TOUCH_FILE  = "C:\Users\Lenovo T460p\.claude\last_claude_call.txt"
$CLAUDE      = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $CLAUDE) { $CLAUDE = "claude" }

# --- PC-Idle-Zeit per Win32 API ---
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class IdleCheck {
    [DllImport("user32.dll")]
    static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    [StructLayout(LayoutKind.Sequential)]
    struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    public static uint IdleSeconds() {
        var info = new LASTINPUTINFO();
        info.cbSize = (uint)Marshal.SizeOf(info);
        GetLastInputInfo(ref info);
        return (uint)(Environment.TickCount - (int)info.dwTime) / 1000;
    }
}
"@ -ErrorAction SilentlyContinue

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Skip wenn PC aktiv (idle < 5 Min)
$idleSec = 9999
try { $idleSec = [IdleCheck]::IdleSeconds() } catch {}
if ($idleSec -lt 300) {
    "[$ts] SKIP — PC aktiv (idle ${idleSec}s)" | Add-Content -Path $LOG -Encoding UTF8
    exit 0
}

# Skip wenn Claude kuerzlich genutzt (Touch-Datei < 25 Min)
if (Test-Path $TOUCH_FILE) {
    $lastCall = (Get-Item $TOUCH_FILE).LastWriteTime
    $ageSec = (New-TimeSpan -Start $lastCall -End (Get-Date)).TotalSeconds
    if ($ageSec -lt 1500) {
        "[$ts] SKIP — letzte Claude-Nutzung vor $([int]$ageSec)s" | Add-Content -Path $LOG -Encoding UTF8
        exit 0
    }
}

# Zufaellige Verzoegerung 0-30 Minuten (verhindert Gleichzeitigkeit mit VPS)
$delay = Get-Random -Minimum 0 -Maximum 1800
Start-Sleep -Seconds $delay

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
try {
    $resp = & $CLAUDE -p "Antworte mit genau einem Wort: OK" 2>&1 | Select-Object -First 3 | Out-String
    $resp = $resp.Trim() -replace "`n", " "
} catch {
    $resp = "FEHLER: $_"
}

$creds = "C:\Users\Lenovo T460p\.claude\.credentials.json"
try {
    $exp = (Get-Content $creds | ConvertFrom-Json).claudeAiOauth.expiresAt
    $expDt = ([DateTimeOffset]::FromUnixTimeMilliseconds($exp)).LocalDateTime.ToString("yyyy-MM-dd HH:mm")
} catch {
    $expDt = "unbekannt"
}

"[$ts] REFRESH — resp='$resp' | token_bis=$expDt | idle=${idleSec}s" | Add-Content -Path $LOG -Encoding UTF8

# Touch-Datei aktualisieren
$ts | Set-Content -Path $TOUCH_FILE -Encoding UTF8
