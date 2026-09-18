$ErrorActionPreference = 'Stop'
$labRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$labUrl = 'http://127.0.0.1:8765'
$labPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path -LiteralPath $labPython)) {
    $labCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $labCommand) { throw 'No se encontró Python. Instala Python 3.11 o superior y vuelve a abrir el laboratorio.' }
    $labPython = $labCommand.Source
}
$labRunning = $false
try {
    $labHealth = Invoke-RestMethod -Uri "$labUrl/api/health" -TimeoutSec 2
    $labRunning = $labHealth.local_only -eq $true
} catch { }
if (-not $labRunning) {
    $labServer = Join-Path $labRoot 'server.py'
    Start-Process -FilePath $labPython -ArgumentList @(('"' + $labServer + '"')) -WorkingDirectory $labRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $labRoot 'servidor.log') -RedirectStandardError (Join-Path $labRoot 'errores.log')
    for ($labAttempt = 0; $labAttempt -lt 90; $labAttempt++) {
        Start-Sleep -Milliseconds 1000
        try {
            $labHealth = Invoke-RestMethod -Uri "$labUrl/api/health" -TimeoutSec 2
            if ($labHealth.local_only -eq $true) { $labRunning = $true; break }
        } catch { }
    }
}
if ($labRunning) { Start-Process $labUrl }
else { throw 'No se pudo iniciar. Revisa errores.log en la carpeta del laboratorio.' }
