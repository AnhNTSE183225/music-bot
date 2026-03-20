# MusicBot Runner - Handles venv setup and execution
Write-Host "🎵 MusicBot" -ForegroundColor Cyan
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
    Write-Host "❌ Python was not found. Install Python 3 and try again." -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    exit 1
}

# Create venv if it doesn't exist
if (-not (Test-Path ".\venv")) {
    Write-Host "Virtual environment not found. Creating..." -ForegroundColor Yellow
    & $pythonExe @pythonArgs -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ Failed to create virtual environment." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        exit 1
    }
    Write-Host "✓ Virtual environment created" -ForegroundColor Green
}

$venvPython = ".\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "❌ venv Python not found at $venvPython" -ForegroundColor Red
    Read-Host "Press Enter to exit..."
    exit 1
}

# Install/update dependencies on first run or if requirements.txt changed
$venvMarker = ".\venv\.installed"
$depsHealthy = $true
& $venvPython -c "import yaml" | Out-Null
if ($LASTEXITCODE -ne 0) {
    $depsHealthy = $false
}

if (-not (Test-Path $venvMarker) -or (Get-Item "requirements.txt").LastWriteTime -gt (Get-Item $venvMarker).LastWriteTime -or -not $depsHealthy) {
    Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Yellow
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ Failed to install dependencies." -ForegroundColor Red
        Read-Host "Press Enter to exit..."
        exit 1
    }
    New-Item $venvMarker -Force | Out-Null
    Write-Host "✓ Dependencies installed" -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting bot..." -ForegroundColor Cyan
Write-Host ""

# Run the bot
& $venvPython bot.py

Read-Host "Press Enter to exit..."