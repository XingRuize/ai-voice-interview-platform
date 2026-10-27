$ErrorActionPreference = 'Continue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$logsDir = Join-Path $projectRoot 'logs'

function Test-Endpoint([string]$Url, [int]$TimeoutSeconds = 3) {
    try {
        return (Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec $TimeoutSeconds).StatusCode -eq 200
    } catch {
        return $false
    }
}

function Find-Python {
    try {
        $candidate = (& py -3.11 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -First 1)
        if ($candidate -and (Test-Path -LiteralPath $candidate.Trim())) {
            return $candidate.Trim()
        }
    } catch {}
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

Write-Host 'Checking the local interview platform...' -ForegroundColor Cyan
$webReady = Test-Endpoint 'http://127.0.0.1:8765/api/health' 8
if (-not $webReady) {
    Write-Host 'The web backend is not running. Trying to restart it.' -ForegroundColor Yellow
    $pythonExe = Find-Python
    if ($pythonExe) {
        New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
        $stdoutLog = Join-Path $logsDir 'web_app.stdout.log'
        $stderrLog = Join-Path $logsDir 'web_app.error.log'
        $env:PYTHONIOENCODING = 'utf-8'
        Start-Process -FilePath $pythonExe -ArgumentList 'web_app.py' -WorkingDirectory $projectRoot `
            -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
        $deadline = (Get-Date).AddSeconds(20)
        do {
            Start-Sleep -Milliseconds 800
            $webReady = Test-Endpoint 'http://127.0.0.1:8765/api/health' 3
        } while (-not $webReady -and (Get-Date) -lt $deadline)
    } else {
        Write-Host 'Python 3.11 was not found. Run the one-click installer again.' -ForegroundColor Red
    }
}

$asrReady = Test-Endpoint 'http://127.0.0.1:8000/docs'
$ttsReady = Test-Endpoint 'http://127.0.0.1:50000/docs'
$qwenReady = Test-Endpoint 'http://127.0.0.1:11434/api/tags'
$webStatus = 'NOT CONNECTED'
$asrStatus = 'NOT CONNECTED'
$ttsStatus = 'NOT CONNECTED'
$qwenStatus = 'NOT CONNECTED - rule fallback will be used'
if ($webReady) { $webStatus = 'OK' }
if ($asrReady) { $asrStatus = 'OK' }
if ($ttsReady) { $ttsStatus = 'OK' }
if ($qwenReady) { $qwenStatus = 'OK' }

Write-Host "Web backend: $webStatus"
Write-Host "Speech recognition: $asrStatus"
Write-Host "Speech synthesis: $ttsStatus"
Write-Host "Qwen dialogue: $qwenStatus"

if ($webReady -and $asrReady -and $ttsReady) {
    Write-Host 'Connection is healthy. Opening the platform.' -ForegroundColor Green
    Start-Process 'http://127.0.0.1:8765/'
    exit 0
}

Write-Host ''
if (-not ($asrReady -and $ttsReady)) {
    Write-Host 'Voice services are not ready. Open Docker Desktop, then run the one-click launcher.' -ForegroundColor Yellow
}
if (-not $webReady) {
    Write-Host 'The web backend could not start. Send logs\web_app.error.log and logs\web_app.log to the maintainer.' -ForegroundColor Red
    $errorLog = Join-Path $logsDir 'web_app.error.log'
    if (Test-Path -LiteralPath $errorLog) {
        Write-Host 'Most recent backend errors:' -ForegroundColor Yellow
        Get-Content -LiteralPath $errorLog -Tail 20
    }
}
exit 1
