[CmdletBinding()]
param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $projectRoot 'install.log'
$minimumFreeGb = 45

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
    "[$(Get-Date -Format s)] $Message" | Add-Content -LiteralPath $logPath -Encoding UTF8
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-WingetPackage([string]$Id, [string]$Label) {
    Write-Step "Installing $Label"
    & winget.exe install --id $Id --exact --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "$Label installation failed with exit code $LASTEXITCODE."
    }
    Refresh-Path
}

function Get-PythonExe {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $value = & $launcher.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $value -and (Test-Path -LiteralPath $value.Trim())) {
            return $value.Trim()
        }
    }
    $candidate = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($candidate -and $candidate.Source -notmatch 'WindowsApps') {
        return $candidate.Source
    }
    return $null
}

function Wait-Docker([int]$Seconds = 600) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            & docker.exe info *> $null
            if ($LASTEXITCODE -eq 0) { return $true }
        } catch {}
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Wait-Endpoint([string]$Url, [int]$Seconds = 1200) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing $Url -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    return $false
}

try {
    Set-Location -LiteralPath $projectRoot
    "[$(Get-Date -Format s)] Installer started" | Set-Content -LiteralPath $logPath -Encoding UTF8

    Write-Step 'Checking Windows and disk space'
    if (-not (Test-Command 'winget.exe')) {
        throw 'winget is missing. Install Microsoft App Installer from Microsoft Store, then run this file again.'
    }
    $driveName = ([System.IO.Path]::GetPathRoot($projectRoot)).TrimEnd('\').TrimEnd(':')
    $drive = Get-PSDrive -Name $driveName
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    Write-Host "Free disk space: $freeGb GB"
    if ($freeGb -lt $minimumFreeGb) {
        throw "At least $minimumFreeGb GB free space is required. Current free space: $freeGb GB."
    }

    Write-Step 'Checking NVIDIA driver'
    if (-not (Test-Command 'nvidia-smi.exe')) {
        throw 'An NVIDIA GPU driver was not found. Install a current NVIDIA driver, restart Windows, then run again.'
    }
    & nvidia-smi.exe --query-gpu=name,driver_version,memory.total --format=csv,noheader

    if ($CheckOnly) {
        Write-Host 'Basic system checks passed.' -ForegroundColor Green
        exit 0
    }

    if (-not (Test-Command 'git.exe')) {
        Install-WingetPackage 'Git.Git' 'Git'
    }
    if (-not (Get-PythonExe)) {
        Install-WingetPackage 'Python.Python.3.11' 'Python 3.11'
    }
    if (-not (Test-Command 'ffmpeg.exe')) {
        Install-WingetPackage 'Gyan.FFmpeg' 'FFmpeg'
    }
    if (-not (Test-Command 'docker.exe')) {
        Install-WingetPackage 'Docker.DockerDesktop' 'Docker Desktop'
    }

    Refresh-Path
    $pythonExe = Get-PythonExe
    if (-not $pythonExe) { throw 'Python was installed but could not be located. Restart Windows and run again.' }

    Write-Step 'Installing the small web application dependency'
    & $pythonExe -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements-client.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }

    Write-Step 'Preparing the official CosyVoice source'
    $cosyServer = Join-Path $projectRoot 'services\CosyVoice\runtime\python\fastapi\server.py'
    if (-not (Test-Path -LiteralPath $cosyServer)) {
        & git.exe submodule update --init --recursive
        if ($LASTEXITCODE -ne 0) { throw 'CosyVoice source download failed.' }
    }

    Write-Step 'Starting Docker Desktop'
    if (-not (Wait-Docker -Seconds 5)) {
        $dockerDesktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
        if (-not (Test-Path -LiteralPath $dockerDesktop)) {
            throw 'Docker Desktop executable was not found after installation.'
        }
        Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
        Write-Host 'Waiting for Docker Desktop (up to 10 minutes)...'
        if (-not (Wait-Docker -Seconds 600)) {
            throw 'Docker Desktop is not ready. Windows may require a restart or WSL 2 setup. Restart and run this installer again.'
        }
    }

    Write-Step 'Building services and downloading local speech models'
    New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'models\modelscope') | Out-Null
    & docker.exe compose -f (Join-Path $projectRoot 'docker-compose.yml') up -d --build
    if ($LASTEXITCODE -ne 0) { throw 'Docker service build/start failed.' }

    Write-Host 'Waiting for SenseVoice and CosyVoice to load (first run can take 10-30 minutes)...'
    $asrReady = Wait-Endpoint 'http://127.0.0.1:8000/docs' 1800
    $ttsReady = Wait-Endpoint 'http://127.0.0.1:50000/docs' 1800
    if (-not ($asrReady -and $ttsReady)) {
        & docker.exe compose -f (Join-Path $projectRoot 'docker-compose.yml') logs --tail 80
        throw 'The speech services did not become ready. Review the Docker logs shown above.'
    }

    Write-Step 'Starting the interview website'
    try {
        $existing = Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8765/' -TimeoutSec 2
    } catch { $existing = $null }
    if (-not $existing -or $existing.StatusCode -ne 200) {
        Start-Process -FilePath $pythonExe -ArgumentList 'web_app.py' -WorkingDirectory $projectRoot -WindowStyle Hidden
        Start-Sleep -Seconds 2
    }
    Start-Process 'http://127.0.0.1:8765/'
    Write-Host "`nInstallation complete. The platform is available at http://127.0.0.1:8765/" -ForegroundColor Green
    Write-Host 'After the first successful installation, the platform can run without Internet access.'
    exit 0
} catch {
    $message = $_.Exception.Message
    "[$(Get-Date -Format s)] ERROR: $message" | Add-Content -LiteralPath $logPath -Encoding UTF8
    Write-Host "`nERROR: $message" -ForegroundColor Red
    Write-Host "Log: $logPath"
    exit 1
}
