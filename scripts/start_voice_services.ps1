$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$modeFile = Join-Path $projectRoot 'runtime_mode.txt'
$mode = if (Test-Path -LiteralPath $modeFile) {
    (Get-Content -LiteralPath $modeFile -Raw).Trim().ToLowerInvariant()
} elseif (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue) {
    'gpu'
} else {
    'cpu'
}

Set-Location -LiteralPath $projectRoot
$ollamaReady = $false
try { $ollamaReady = (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2).StatusCode -eq 200 } catch {}
if (-not $ollamaReady) {
    $ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
    $ollamaExe = if ($ollama) { $ollama.Source } else { Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
    if (Test-Path -LiteralPath $ollamaExe) {
        Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
    } else {
        Write-Warning 'Ollama is not installed. The interview will use the safe rule fallback.'
    }
}
if ($mode -eq 'gpu') {
    Write-Host 'Starting GPU voice services...'
    & docker.exe compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
} else {
    Write-Host 'Starting CPU voice services...'
    & docker.exe compose -f docker-compose.yml up -d
}
exit $LASTEXITCODE
