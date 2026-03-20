# MusicBot Runner - Handles venv setup and execution
Write-Host "🎵 MusicBot" -ForegroundColor Cyan
Write-Host ""

# Create venv if it doesn't exist
if (-not (Test-Path ".\venv")) {
    Write-Host "Virtual environment not found. Creating..." -ForegroundColor Yellow
    python -m venv venv
    Write-Host "✓ Virtual environment created" -ForegroundColor Green
}

# Activate virtual environment
Write-Host "Activating virtual environment..." -ForegroundColor Yellow
.\venv\Scripts\Activate.ps1

# Install/update dependencies on first run or if requirements.txt changed
$venvMarker = ".\venv\.installed"
if (-not (Test-Path $venvMarker) -or (Get-Item "requirements.txt").LastWriteTime -gt (Get-Item $venvMarker).LastWriteTime) {
    Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Yellow
    pip install -r requirements.txt
    New-Item $venvMarker -Force | Out-Null
    Write-Host "✓ Dependencies installed" -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting bot..." -ForegroundColor Cyan
Write-Host ""

# Run the bot
python bot.py

Read-Host "Press Enter to exit..."