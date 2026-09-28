param(
    [switch]$NoPauseOnError,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appPath = Join-Path $scriptDir "app.py"
$runtimeTemp = Join-Path $scriptDir ".tmp"
$logPath = Join-Path $runtimeTemp "run-latest.log"
$serverUrl = "http://127.0.0.1:5178/"

function Stop-GeoInventoryLaunch {
    param(
        [string]$Message,
        [object[]]$Details = @()
    )

    Write-Host ""
    Write-Host "[GeoInventory] Startup failed: $Message" -ForegroundColor Red
    foreach ($detail in $Details) {
        if ($null -ne $detail) {
            Write-Host ([string]$detail) -ForegroundColor DarkRed
        }
    }
    Write-Host "Log: $logPath" -ForegroundColor Yellow
    if (-not $NoPauseOnError) {
        try {
            [void](Read-Host "Press Enter to close this window")
        }
        catch {
            # A non-interactive host cannot pause; the log still preserves the error.
        }
    }
    exit 1
}

function Test-GeoInventoryServer {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $serverUrl -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match "GeoInventory"
    }
    catch {
        return $false
    }
}

try {
    Set-Location -LiteralPath $scriptDir
    New-Item -ItemType Directory -Path $runtimeTemp -Force | Out-Null
    $env:TEMP = $runtimeTemp
    $env:TMP = $runtimeTemp
    $env:PYTHONUNBUFFERED = "1"

    if (-not (Test-Path -LiteralPath $appPath -PathType Leaf)) {
        Stop-GeoInventoryLaunch "app.py was not found." @($appPath)
    }

    if (Test-GeoInventoryServer) {
        Write-Host "[GeoInventory] The service is already running at $serverUrl" -ForegroundColor Green
        if (-not $NoBrowser) {
            Start-Process $serverUrl
        }
        exit 0
    }

    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] GeoInventory launcher started." |
        Set-Content -LiteralPath $logPath -Encoding Unicode

    $pythonCandidates = @()
    if ($env:GEOINVENTORY_PYTHON) {
        $pythonCandidates += $env:GEOINVENTORY_PYTHON
    }
    $pythonCandidates += (Join-Path $scriptDir ".venv\Scripts\python.exe")
    if ($env:CONDA_PREFIX) {
        $pythonCandidates += (Join-Path $env:CONDA_PREFIX "python.exe")
    }
    $pythonCommands = Get-Command python -All -ErrorAction SilentlyContinue
    foreach ($pythonCommand in $pythonCommands) {
        $pythonCandidates += $pythonCommand.Source
    }
    if ($env:USERPROFILE) {
        $pythonCandidates += (Join-Path $env:USERPROFILE "anaconda3\python.exe")
        $pythonCandidates += (Join-Path $env:USERPROFILE "miniconda3\python.exe")
    }
    if ($env:ProgramData) {
        $pythonCandidates += (Join-Path $env:ProgramData "anaconda3\python.exe")
        $pythonCandidates += (Join-Path $env:ProgramData "miniconda3\python.exe")
    }
    foreach ($drive in [System.IO.DriveInfo]::GetDrives()) {
        if ($drive.IsReady -and $drive.DriveType -eq [System.IO.DriveType]::Fixed) {
            $pythonCandidates += (Join-Path $drive.RootDirectory.FullName "anaconda3\python.exe")
            $pythonCandidates += (Join-Path $drive.RootDirectory.FullName "miniconda3\python.exe")
        }
    }

    $availablePython = @($pythonCandidates |
        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Select-Object -Unique)

    if ($availablePython.Count -eq 0) {
        Stop-GeoInventoryLaunch "Python was not found." @(
            "Install Python 3.10 or newer, or set GEOINVENTORY_PYTHON to python.exe."
        )
    }

    $pythonExe = $null
    $lastDiagnostics = @()
    foreach ($candidate in $availablePython) {
        Write-Host "[GeoInventory] Checking runtime: $candidate" -ForegroundColor Cyan
        $diagnosticsCommandLine = ('""{0}" "{1}" --diagnostics 2>&1"' -f $candidate, $appPath)
        $diagnostics = @(& $env:ComSpec /d /s /c $diagnosticsCommandLine)
        $diagnosticsExitCode = $LASTEXITCODE
        "`r`nRuntime candidate: $candidate" | Add-Content -LiteralPath $logPath -Encoding Unicode
        $diagnostics | Add-Content -LiteralPath $logPath -Encoding Unicode
        if ($diagnosticsExitCode -eq 0) {
            $pythonExe = $candidate
            break
        }
        $lastDiagnostics = $diagnostics
        Write-Host "[GeoInventory] Skipping this Python because required packages are missing." -ForegroundColor Yellow
    }

    if (-not $pythonExe) {
        $preferredPython = $availablePython[0]
        Stop-GeoInventoryLaunch "Python dependencies are incomplete." @(
            "Checked runtimes: $($availablePython -join ', ')",
            $lastDiagnostics,
            "Repair command: `"$preferredPython`" -m pip install -r `"$(Join-Path $scriptDir 'requirements.txt')`""
        )
    }

    Write-Host "[GeoInventory] Starting service at $serverUrl" -ForegroundColor Green
    Write-Host "Keep this window open. Press Ctrl+C to stop the service." -ForegroundColor DarkGray
    # Merge the native streams inside cmd.exe. Windows PowerShell otherwise
    # decorates Flask's normal stderr startup banner as a NativeCommandError.
    $commandLine = ('""{0}" "{1}" 2>&1"' -f $pythonExe, $appPath)
    & $env:ComSpec /d /s /c $commandLine | Tee-Object -FilePath $logPath -Append
    $appExitCode = $LASTEXITCODE

    if ($appExitCode -ne 0) {
        Stop-GeoInventoryLaunch "The service exited with code $appExitCode."
    }
}
catch {
    try {
        $_ | Out-String | Add-Content -LiteralPath $logPath -Encoding Unicode
    }
    catch {
        # If logging itself fails, the console message remains available.
    }
    Stop-GeoInventoryLaunch $_.Exception.Message @($_.ScriptStackTrace)
}
