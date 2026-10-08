# MusicBot Windows PowerShell Stopper - Stops MusicBot cleanly

Write-Host "Stopping MusicBot..." -ForegroundColor Cyan

# Find any python process running bot.py or listening on port 8000
$processes = Get-CimInstance Win32_Process | Where-Object { 
    $_.Name -like "python*.exe" -and $_.CommandLine -like "*bot.py*"
}

if (-not $processes) {
    # Check if a process is listening on port 8000
    $conn = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $processes = Get-CimInstance Win32_Process -Filter "ProcessId = $($conn.OwningProcess)"
    }
}

if ($processes) {
    foreach ($proc in $processes) {
        Write-Host "Stopping process $($proc.ProcessId) ($($proc.Name))..." -ForegroundColor Yellow
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    Write-Host "MusicBot stopped successfully." -ForegroundColor Green
} else {
    Write-Host "MusicBot is not currently running." -ForegroundColor Yellow
}
