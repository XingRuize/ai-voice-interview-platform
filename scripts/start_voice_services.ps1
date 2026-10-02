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
if ($mode -eq 'gpu') {
    Write-Host 'Starting GPU voice services...'
    & docker.exe compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
} else {
    Write-Host 'Starting CPU voice services...'
    & docker.exe compose -f docker-compose.yml up -d
}
exit $LASTEXITCODE
