$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$Url = "http://127.0.0.1:8050"

function Wait-ForExitPrompt {
    if ($Host.Name -ne "ConsoleHost") {
        return
    }
    Write-Host ""
    Read-Host "Press Enter to close this window"
}

function Test-DashboardServer {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 1 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-DashboardProcess {
    try {
        $connection = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8050 -State Listen -ErrorAction Stop | Select-Object -First 1
        if (-not $connection) {
            return $null
        }
        return Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
    } catch {
        return $null
    }
}

Write-Host "FL Stats"
Write-Host "Workspace: $Root"

python -c "import dash, dash_bootstrap_components" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing dashboard dependencies..."
    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Dependency installation failed."
        Wait-ForExitPrompt
        exit 1
    }
}

if (Test-DashboardServer) {
    $ExistingProcess = Get-DashboardProcess
    Write-Host "A dashboard is already running at $Url"
    if ($ExistingProcess) {
        Write-Host "Server process: $($ExistingProcess.ProcessName) PID $($ExistingProcess.Id)"
        $Choice = Read-Host "Press S to stop it, O to open it, or Enter to leave it running"
        if ($Choice -match "^[sS]") {
            Stop-Process -Id $ExistingProcess.Id -Force
            Start-Sleep -Milliseconds 500
        } else {
            Start-Process $Url
            Wait-ForExitPrompt
            return
        }
    } else {
        Write-Host "Close that server window first if you want this window to own the server."
        Start-Process $Url
        Wait-ForExitPrompt
        return
    }
}

if (Test-DashboardServer) {
    Write-Host "The dashboard is still responding at $Url"
    Start-Process $Url
    Wait-ForExitPrompt
    return
}

Write-Host "Starting local dashboard server."
Write-Host "Keep this window open while using the dashboard. Close it or press Ctrl+C to stop the server."

$OpenBrowserJob = Start-Job -ScriptBlock {
    param($DashboardUrl)
    for ($i = 0; $i -lt 50; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $DashboardUrl -TimeoutSec 1 | Out-Null
            Start-Process $DashboardUrl
            return
        } catch {
            Start-Sleep -Milliseconds 300
        }
    }
} -ArgumentList $Url

try {
    python flp_dashboard.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Dashboard failed to start:"
        Write-Host "Python exited with code $LASTEXITCODE"
        Wait-ForExitPrompt
    }
} finally {
    if ($OpenBrowserJob.State -eq "Running") {
        Stop-Job $OpenBrowserJob | Out-Null
    }
    Remove-Job $OpenBrowserJob -Force | Out-Null
}
