# MusicBot Runner - Handles venv setup and execution
$logsDir = Join-Path (Get-Location) "logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir | Out-Null
}
$startLogFile = Join-Path $logsDir "start.log"

function Write-Log {
    param(
        [Parameter(Mandatory=$false)] [string]$Message = "",
        [string]$ForegroundColor
    )
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $fileMsg = if ($Message) { "[$ts] $Message" } else { "" }
    if ($ForegroundColor) {
        Write-Host $Message -ForegroundColor $ForegroundColor
    } else {
        Write-Host $Message
    }
    if ($startLogFile) {
        Add-Content -Path $startLogFile -Value $fileMsg -Encoding utf8
    }
}

Write-Log "MusicBot" -ForegroundColor Cyan
Write-Log "Logging start session to: $startLogFile" -ForegroundColor Cyan
Write-Log ""

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
    Write-Log "ERROR: Python was not found. Install Python 3 and try again." -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    exit 1
}

# Create venv if it doesn't exist
if (-not (Test-Path ".\venv")) {
    Write-Log "Virtual environment not found. Creating..." -ForegroundColor Yellow
    & $pythonExe @pythonArgs -m venv venv 2>&1 | ForEach-Object {
        $line = $_.ToString()
        Write-Host $line
        Add-Content -Path $startLogFile -Value $line -Encoding utf8
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERROR: Failed to create virtual environment." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        exit 1
    }
    Write-Log "OK: Virtual environment created" -ForegroundColor Green
}

$venvPythonPath = Resolve-Path ".\venv\Scripts\python.exe" -ErrorAction SilentlyContinue
if (-not $venvPythonPath) {
    Write-Log "ERROR: venv Python not found at .\venv\Scripts\python.exe" -ForegroundColor Red
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
    Write-Log "Installing dependencies from requirements.txt..." -ForegroundColor Yellow
    & "$venvPython" "-m" "pip" "install" "--upgrade" "pip" 2>&1 | ForEach-Object {
        $line = $_.ToString()
        Write-Host $line
        Add-Content -Path $startLogFile -Value $line -Encoding utf8
    }
    & "$venvPython" "-m" "pip" "install" "-r" "requirements.txt" 2>&1 | ForEach-Object {
        $line = $_.ToString()
        Write-Host $line
        Add-Content -Path $startLogFile -Value $line -Encoding utf8
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Log "ERROR: Failed to install dependencies." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        exit 1
    }
    New-Item $venvMarker -Force | Out-Null
    Write-Log "OK: Dependencies installed" -ForegroundColor Green
}

$nodeExePath = ".\venv\Scripts\node.exe"
if (-not (Test-Path $nodeExePath)) {
    Write-Log "Downloading portable Node.js for yt-dlp JS challenges..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://nodejs.org/dist/v20.11.1/win-x64/node.exe" -OutFile $nodeExePath
    Write-Log "OK: Node.js downloaded" -ForegroundColor Green
}

Write-Log ""
Write-Log "Checking for pip and yt-dlp updates..." -ForegroundColor Cyan
& "$venvPython" "-m" "pip" "install" "-U" "pip" "yt-dlp" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    Write-Host $line
    Add-Content -Path $startLogFile -Value $line -Encoding utf8
}

Write-Log ""
Write-Log "Starting bot..." -ForegroundColor Cyan
Write-Log ""

# Load .env into process environment if present
if (Test-Path ".\.env") {
    Get-Content ".\.env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim()
            if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
                $val = $val.Substring(1, $val.Length - 2)
            }
            [System.Environment]::SetEnvironmentVariable($key, $val, "Process")
        }
    }
}

# Override with DEV_DISCORD_* if set to allow isolated local development
if ($env:DEV_DISCORD_TOKEN) {
    Write-Log "[DEV] Detected DEV_DISCORD_TOKEN: Overriding DISCORD_TOKEN" -ForegroundColor Magenta
    $env:DISCORD_TOKEN = $env:DEV_DISCORD_TOKEN
}
if ($env:DEV_DISCORD_CLIENT_ID) {
    Write-Log "[DEV] Detected DEV_DISCORD_CLIENT_ID: Overriding DISCORD_CLIENT_ID" -ForegroundColor Magenta
    $env:DISCORD_CLIENT_ID = $env:DEV_DISCORD_CLIENT_ID
}
if ($env:DEV_DISCORD_CLIENT_SECRET) {
    Write-Log "[DEV] Detected DEV_DISCORD_CLIENT_SECRET: Overriding DISCORD_CLIENT_SECRET" -ForegroundColor Magenta
    $env:DISCORD_CLIENT_SECRET = $env:DEV_DISCORD_CLIENT_SECRET
}

# Default to DEBUG runtime mode for local development via start.ps1
$runtimeMode = "debug"
if ($env:MUSICBOT_RUNTIME_MODE) {
    $runtimeMode = $env:MUSICBOT_RUNTIME_MODE.ToLower()
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$prodLogFile = Join-Path $logsDir "musicbot-prod.log"

if ($runtimeMode -eq "debug") {
    $debugLogFile = Join-Path $logsDir "musicbot-debug-$timestamp.log"
    $env:MUSICBOT_RUNTIME_MODE = "debug"
    $env:MUSICBOT_LOG_LEVEL = "DEBUG"
    $env:MUSICBOT_PLAYBACK_DEBUG_METRICS = "true"
    $env:MUSICBOT_LOG_FILE = $debugLogFile

    Write-Log "Runtime mode: DEBUG" -ForegroundColor Yellow
    Write-Log "Debug log file: $debugLogFile" -ForegroundColor Yellow
    Write-Log "Log level override: DEBUG" -ForegroundColor Yellow
} else {
    $env:MUSICBOT_RUNTIME_MODE = "prod"
    $env:MUSICBOT_LOG_LEVEL = "INFO"
    $env:MUSICBOT_PLAYBACK_DEBUG_METRICS = "false"
    $env:MUSICBOT_LOG_FILE = $prodLogFile

    Write-Log "Runtime mode: PROD" -ForegroundColor Green
    Write-Log "Production log file: $prodLogFile" -ForegroundColor Green
    Write-Log "Log level override: INFO" -ForegroundColor Green
}

Write-Log ""

# Add venv to PATH so yt-dlp can find node.exe
$env:PATH = "$((Resolve-Path .\venv\Scripts).Path);$env:PATH"

# Check and clean up orphaned MusicBot processes and port 8000 listeners before launch
Write-Log "Checking for existing MusicBot processes..." -ForegroundColor Cyan
$killedCount = 0
$oldProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { 
    $_.ProcessId -ne $PID -and $_.Name -like "python*.exe" -and $_.CommandLine -like "*bot.py*"
}
foreach ($p in $oldProcesses) {
    Write-Log "Stopping existing MusicBot process (PID $($p.ProcessId))..." -ForegroundColor Yellow
    $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
    if ($proc) {
        $null = $proc.CloseMainWindow()
    }
    # Wait up to 3 seconds for graceful shutdown
    $waited = 0
    while ($proc -and -not $proc.HasExited -and $waited -lt 3) {
        Start-Sleep -Seconds 1
        $waited++
        $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
    }
    if ($proc -and -not $proc.HasExited) {
        Write-Log "Process (PID $($p.ProcessId)) did not exit gracefully within 3s. Force terminating..." -ForegroundColor Red
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    } else {
        Write-Log "Process (PID $($p.ProcessId)) stopped cleanly." -ForegroundColor Green
    }
    $killedCount++
}

# Also ensure port 8000 (FastAPI API server) is free
$port8000 = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $port8000) {
    if ($conn.OwningProcess -ne $PID -and $conn.OwningProcess -gt 0) {
        Write-Log "Freeing port 8000 from lingering process (PID $($conn.OwningProcess))..." -ForegroundColor Yellow
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        $killedCount++
    }
}

if ($killedCount -eq 0) {
    Write-Log "No lingering processes found. Environment clean." -ForegroundColor Green
}

Write-Log "Launching MusicBot process (logging to $startLogFile and $env:MUSICBOT_LOG_FILE)..." -ForegroundColor Cyan
Write-Log ""

# Run the bot and pipe all output to console and start.log in real time
$env:PYTHONUNBUFFERED = "1"
& "$venvPython" ".\bot.py" 2>&1 | ForEach-Object {
    $line = $_.ToString()
    Write-Host $line
    Add-Content -Path $startLogFile -Value $line -Encoding utf8
}
$botExit = $LASTEXITCODE

Write-Log "Bot exited with code $botExit." -ForegroundColor Yellow
if ($botExit -ne 0 -and $botExit -ne 130) {
    Read-Host "Press Enter to exit..."
}