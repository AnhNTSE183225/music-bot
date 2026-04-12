# MusicBot Runner - Handles venv setup and execution
Write-Host "MusicBot" -ForegroundColor Cyan
Write-Host ""

# Select a Python launcher for initial venv creation
$pythonExe = ""
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonExe = "py"
    $pythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = "python"
    $pythonArgs = @()
} else {
    Write-Host "ERROR: Python was not found. Install Python 3 and try again." -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    exit 1
}

# Create venv if it doesn't exist
if (-not (Test-Path ".\venv")) {
    Write-Host "Virtual environment not found. Creating..." -ForegroundColor Yellow
    & $pythonExe @pythonArgs -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to create virtual environment." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        exit 1
    }
    Write-Host "OK: Virtual environment created" -ForegroundColor Green
}

$venvPythonPath = Resolve-Path ".\venv\Scripts\python.exe" -ErrorAction SilentlyContinue
if (-not $venvPythonPath) {
    Write-Host "ERROR: venv Python not found at .\venv\Scripts\python.exe" -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    exit 1
}
[string]$venvPython = $venvPythonPath.Path

# Install/update dependencies on first run, on requirements change, or when yaml is missing
$venvMarker = ".\venv\.installed"
$depsHealthy = $true
& "$venvPython" "-c" "import yaml" | Out-Null
if ($LASTEXITCODE -ne 0) {
    $depsHealthy = $false
}

$requirementsChanged = $false
if (Test-Path $venvMarker) {
    $requirementsChanged = (Get-Item "requirements.txt").LastWriteTime -gt (Get-Item $venvMarker).LastWriteTime
}

if (-not (Test-Path $venvMarker) -or $requirementsChanged -or -not $depsHealthy) {
    Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Yellow
    & "$venvPython" "-m" "pip" "install" "--upgrade" "pip"
    & "$venvPython" "-m" "pip" "install" "-r" "requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install dependencies." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        exit 1
    }
    New-Item $venvMarker -Force | Out-Null
    Write-Host "OK: Dependencies installed" -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting bot..." -ForegroundColor Cyan
Write-Host ""

# Resolve runtime mode from config.yaml (default: prod)
$runtimeMode = "prod"
$runtimeModeOutput = & "$venvPython" "-c" "import yaml; c=yaml.safe_load(open('config.yaml','r',encoding='utf-8')) or {}; print(str((c.get('runtime',{}) or {}).get('mode','prod')).strip().lower())"
if ($LASTEXITCODE -eq 0 -and $runtimeModeOutput) {
    $modeCandidate = $runtimeModeOutput.Trim().ToLower()
    if ($modeCandidate -in @("debug", "prod")) {
        $runtimeMode = $modeCandidate
    }
}

# Create external log file targets
$logsDir = Join-Path (Get-Location) "logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$prodLogFile = Join-Path $logsDir "musicbot-prod.log"

if ($runtimeMode -eq "debug") {
    $debugLogFile = Join-Path $logsDir "musicbot-debug-$timestamp.log"
    $env:MUSICBOT_RUNTIME_MODE = "debug"
    $env:MUSICBOT_LOG_LEVEL = "DEBUG"
    $env:MUSICBOT_PLAYBACK_DEBUG_METRICS = "true"
    $env:MUSICBOT_LOG_FILE = $debugLogFile

    Write-Host "Runtime mode: DEBUG" -ForegroundColor Yellow
    Write-Host "Debug log file: $debugLogFile" -ForegroundColor Yellow
    Write-Host "Log level override: DEBUG" -ForegroundColor Yellow
} else {
    $env:MUSICBOT_RUNTIME_MODE = "prod"
    $env:MUSICBOT_LOG_LEVEL = "INFO"
    $env:MUSICBOT_PLAYBACK_DEBUG_METRICS = "false"
    $env:MUSICBOT_LOG_FILE = $prodLogFile

    Write-Host "Runtime mode: PROD" -ForegroundColor Green
    Write-Host "Production log file: $prodLogFile" -ForegroundColor Green
    Write-Host "Log level override: INFO" -ForegroundColor Green
}

Write-Host ""

# Run the bot
& "$venvPython" ".\bot.py"

Read-Host "Press Enter to exit..."