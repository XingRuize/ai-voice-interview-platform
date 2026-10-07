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
$deadline = (Get-Date).AddSeconds(45)
do {
    try {
        $ollamaReady = (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2).StatusCode -eq 200
    } catch { $ollamaReady = $false }
    if (-not $ollamaReady) { Start-Sleep -Seconds 2 }
} while (-not $ollamaReady -and (Get-Date) -lt $deadline)
if ($ollamaReady) {
    Write-Host 'Warming up Qwen for realtime semantic responses...'
    $warmup = @{
        model = 'qwen3:4b'; prompt = '只回答：就绪 /no_think'; stream = $false; think = $false
        keep_alive = '30m'; options = @{ num_predict = 8; num_ctx = 2048 }
    } | ConvertTo-Json -Depth 5
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/generate' -Method Post `
            -ContentType 'application/json; charset=utf-8' -Body $warmup -TimeoutSec 120 | Out-Null
        $loaded = (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/ps' -TimeoutSec 5).models |
            Where-Object { $_.model -eq 'qwen3:4b' } | Select-Object -First 1
        if ($loaded -and [double]$loaded.size_vram -gt 0) {
            Write-Host 'Qwen is ready on GPU.' -ForegroundColor Green
        } else {
            Write-Warning 'Qwen is running without confirmed GPU acceleration.'
        }
    } catch {
        Write-Warning "Qwen warm-up failed; rule fallback remains available: $($_.Exception.Message)"
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
