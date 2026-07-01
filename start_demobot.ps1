# start_demobot.ps1 -- Demobot starten oder neustarten
# Aufruf: cd C:\projekte\demobot ; .\start_demobot.ps1 [-Force]
# -Force: laufenden Bot auch beenden (default: nicht killen wenn gesund)

param([switch]$Force)

$PYTHON  = "C:\Users\Lenovo T460p\AppData\Local\Programs\Python\Python39\python.exe"
$SCRIPT  = "C:\projekte\bot-core\bot_mm.py"
$WORKDIR = "C:\projekte\demobot"
$env:BOT_BASE_DIR = "C:\projekte\demobot"
$LOG_OUT = "$WORKDIR\logs\bot.log"
$LOG_ERR = "$WORKDIR\logs\bot_err.log"
$LOG_ERR_HIST = "$WORKDIR\logs\bot_err_history.log"
$PID_FILE = "$WORKDIR\.pid"

# Pruefen ob Bot bereits laeuft (via PID-Datei)
if (Test-Path $PID_FILE) {
    $savedPid = Get-Content $PID_FILE -ErrorAction SilentlyContinue
    if ($savedPid) {
        $runningProc = Get-Process -Id ([int]$savedPid) -ErrorAction SilentlyContinue
        if ($runningProc -and $runningProc.ProcessName -match "python") {
            if (-not $Force) {
                Write-Host "Bot laeuft bereits (PID $savedPid) - kein Neustart. Nutze -Force um zu erzwingen."
                exit 0
            }
            # -Force: Bot und alle Kinder (Claude/node.exe) killen
            Write-Host "Stoppe Bot (PID $savedPid) + Claude-Prozesse..."
            $ts = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
            Add-Content -Path $LOG_ERR_HIST -Value "=== RESTART $ts ==="
            Get-Content $LOG_ERR -ErrorAction SilentlyContinue | Add-Content -Path $LOG_ERR_HIST
            Add-Content -Path $LOG_ERR_HIST -Value ""
            # Prozesskette killen (Python + alle Kinder inkl. node.exe/claude)
            & taskkill /F /T /PID $savedPid 2>$null
            Start-Sleep -Seconds 2
        }
    }
}

# Kein laufender Bot: auch via WMI sicherstellen
$wmiProcs = Get-WmiObject Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -match "demobot_mm|bot_mm" }
if ($wmiProcs) {
    foreach ($p in $wmiProcs) {
        Write-Host "Stoppe verwaisten Prozess PID $($p.ProcessId)..."
        & taskkill /F /T /PID $p.ProcessId 2>$null
    }
    Start-Sleep -Seconds 2
}

# History sichern wenn kein Force (Bot war tot, kein Restart-Header noetig)
if (-not $Force) {
    if (Test-Path $LOG_ERR) {
        $ts = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
        Add-Content -Path $LOG_ERR_HIST -Value "=== AUTOSTART $ts (Bot war down) ==="
        Get-Content $LOG_ERR | Add-Content -Path $LOG_ERR_HIST
        Add-Content -Path $LOG_ERR_HIST -Value ""
    }
}

New-Item -ItemType Directory "$WORKDIR\logs" -Force | Out-Null

Start-Process -FilePath $PYTHON -ArgumentList @($SCRIPT) `
    -WorkingDirectory $WORKDIR `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LOG_OUT `
    -RedirectStandardError  $LOG_ERR

Start-Sleep -Seconds 6
$neu = Get-WmiObject Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -match "demobot_mm|bot_mm" }
if ($neu) {
    Write-Host "Gestartet: PID $($neu.ProcessId)"
    Get-Content $LOG_ERR -Tail 5
} else {
    Write-Host "FEHLER: Prozess nicht gestartet - pruefen: $LOG_ERR"
}
