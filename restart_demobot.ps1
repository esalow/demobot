# restart_demobot.ps1 — Sauberer Neustart des Demobot
# Aufruf: cd C:\projekte\demobot ; .\restart_demobot.ps1

$WORK_DIR = "C:\projekte\demobot"
$PYTHON   = "C:\Users\Lenovo T460p\AppData\Local\Programs\Python\Python39\python.exe"
$CLAUDE   = "C:\Users\Lenovo T460p\AppData\Roaming\npm\claude.cmd"
$ENV_FILE = "$WORK_DIR\.env"

Write-Host "`n=== DEMOBOT RESTART ===" -ForegroundColor Cyan
Write-Host "`n--- Checks ---"
$ok = $true
if (Test-Path $ENV_FILE) { Write-Host "  [OK] .env" }
else { Write-Host "  [!!] .env fehlt" -ForegroundColor Red; $ok = $false }
if (Test-Path $PYTHON) { Write-Host "  [OK] python.exe" }
else { Write-Host "  [!!] python.exe fehlt" -ForegroundColor Red; $ok = $false }
if (Test-Path $CLAUDE) { Write-Host "  [OK] claude.cmd" }
else { Write-Host "  [!!] claude.cmd fehlt" -ForegroundColor Red; $ok = $false }
if (-not $ok) { Write-Host "`nAbbruch." -ForegroundColor Red; exit 1 }

# Laufende Demobot-Instanzen stoppen
Write-Host "`n--- Stoppe laufende Demobot-Instanzen ---"
$killed = 0
Get-Process python, pythonw -ErrorAction SilentlyContinue | ForEach-Object {
    $id  = $_.Id
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $id").CommandLine
    if ($cmd -match "demobot_mm") {
        Write-Host "  Stoppe PID $id"
        taskkill /F /T /PID $id | Out-Null
        $killed++
    }
}
if ($killed -eq 0) { Write-Host "  Keine laufende Instanz." }
Start-Sleep -Seconds 3

# Starten
Write-Host "`n--- Starte Demobot ---"
$proc = Start-Process `
    -FilePath $PYTHON `
    -ArgumentList @("demobot_mm.py") `
    -WorkingDirectory $WORK_DIR `
    -WindowStyle Hidden `
    -PassThru
Write-Host "  PID: $($proc.Id)"

# Verbindung abwarten (max 15s)
Write-Host "  Warte auf Mattermost-Verbindung..."
$connected = $false
$i = 0
while ($i -lt 15) {
    Start-Sleep -Seconds 1; $i++
    if ($proc.HasExited) {
        Write-Host "  [!!] Prozess sofort beendet." -ForegroundColor Red; exit 1
    }
    $net = Get-NetTCPConnection -OwningProcess $proc.Id -State Established -ErrorAction SilentlyContinue
    foreach ($c in $net) {
        if ($c.RemotePort -eq 443) { $connected = $true; break }
    }
    if ($connected) { break }
}

Write-Host "`n--- Status ---"
if ($connected) { Write-Host "  [OK] Verbunden mit Mattermost" -ForegroundColor Green }
else { Write-Host "  [!!] Keine Verbindung nach 15s — prüfe .env und Netzwerk" -ForegroundColor Yellow }

$count = 0
Get-Process python, pythonw -ErrorAction SilentlyContinue | ForEach-Object {
    $id  = $_.Id
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $id").CommandLine
    if ($cmd -match "demobot_mm") { $count++ }
}
if ($count -eq 1) { Write-Host "  [OK] Genau 1 Demobot-Instanz laeuft" }
else { Write-Host "  [!!] $count Instanzen laufen" -ForegroundColor Red }

Write-Host "`n=== FERTIG ===" -ForegroundColor Cyan
