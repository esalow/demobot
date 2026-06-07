# start_all.ps1 — Alle 3 Demobot-Instanzen starten/neustarten
# Aufruf: .\start_all.ps1

$PYTHON = "python"
$SCRIPT = "c:\projekte\demobot\demobot_mm.py"
$DIRS = @(
    "c:\projekte\demobot",
    "c:\projekte\demobot2",
    "c:\projekte\demobot3"
)

# Alle laufenden Instanzen stoppen
Write-Host "Stoppe laufende Instanzen..."
Get-WmiObject Win32_Process -Filter "name='python.exe'" | Where-Object {
    $_.CommandLine -like "*demobot_mm*"
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "  PID $($_.ProcessId) gestoppt"
}
Start-Sleep -Seconds 2

# Alle 3 Instanzen starten
foreach ($dir in $DIRS) {
    $name = Split-Path $dir -Leaf
    $logOut = "$dir\logs\bot.log"
    $logErr = "$dir\logs\bot_err.log"
    Start-Process -FilePath $PYTHON -ArgumentList $SCRIPT `
        -WorkingDirectory $dir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $logOut `
        -RedirectStandardError $logErr
    Start-Sleep -Seconds 1
    Write-Host "  $name gestartet"
}

Start-Sleep -Seconds 3

# PIDs anzeigen
Write-Host "`nLaufende Instanzen:"
Get-WmiObject Win32_Process -Filter "name='python.exe'" | Where-Object {
    $_.CommandLine -like "*demobot_mm*"
} | ForEach-Object {
    $cwd = $_.CommandLine
    Write-Host "  PID $($_.ProcessId) - $cwd"
}
