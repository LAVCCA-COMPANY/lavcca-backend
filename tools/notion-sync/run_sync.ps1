# LAVCCA (라브카) Notion -> Obsidian production incremental sync: Task Scheduler wrapper.
#
# Runs:  <python> notion_sync.py --apply-updates   (in this script's directory)
# Logs:  logs\scheduler\sync_YYYYMMDD_HHMMSS.log  (started_at, finished_at, exit_code, stdout/stderr)
# Exit:  the python exit code is passed through to Task Scheduler (LastTaskResult).
#
# Never logs NOTION_TOKEN: notion_sync.py only prints the token SOURCE path, and this
# wrapper does not read, set, or echo any secret.
#
# If the scheduler log cannot be created, a fallback log is used under
# %LOCALAPPDATA%\LAVCCA\scheduler-fallback; if that also fails the sync still runs
# (its own JSON report is written to logs\apply_updates_*.json). Logging is never a
# reason to skip, half-run, or alter a sync.
param(
    [string]$PythonExe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
)

$ErrorActionPreference = "Stop"
$SyncDir = $PSScriptRoot
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogFile = $null

function Write-Log([string]$Line) {
    if ($script:LogFile) {
        try { Add-Content -LiteralPath $script:LogFile -Value $Line -Encoding UTF8 } catch { $script:LogFile = $null }
    }
}

foreach ($dir in @((Join-Path $SyncDir "logs\scheduler"), (Join-Path $env:LOCALAPPDATA "LAVCCA\scheduler-fallback"))) {
    try {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $candidate = Join-Path $dir "sync_$Stamp.log"
        New-Item -ItemType File -Path $candidate -ErrorAction Stop | Out-Null
        $LogFile = $candidate
        break
    } catch { }
}

$startedAt = (Get-Date).ToString("o")
Write-Log "started_at: $startedAt"
Write-Log "sync_dir: $SyncDir"
Write-Log "python: $PythonExe"
Write-Log "command: notion_sync.py --apply-updates"

$exitCode = 1
$outFile = Join-Path $env:TEMP "lavcca_sync_$Stamp.out"
$errFile = Join-Path $env:TEMP "lavcca_sync_$Stamp.err"
try {
    if (-not (Test-Path -LiteralPath $PythonExe)) { throw "python executable not found: $PythonExe" }
    if (-not (Test-Path -LiteralPath (Join-Path $SyncDir "notion_sync.py"))) { throw "notion_sync.py not found in $SyncDir" }
    Set-Location -LiteralPath $SyncDir
    $env:PYTHONIOENCODING = "utf-8"
    $p = Start-Process -FilePath $PythonExe -ArgumentList @("notion_sync.py", "--apply-updates") `
        -WorkingDirectory $SyncDir -NoNewWindow -PassThru `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $null = $p.Handle          # keep the handle so ExitCode is available after exit
    $p.WaitForExit()
    $exitCode = $p.ExitCode
} catch {
    Write-Log "wrapper_error: $($_.Exception.Message)"
    $exitCode = 9
} finally {
    foreach ($pair in @(@("stdout", $outFile), @("stderr", $errFile))) {
        if (Test-Path -LiteralPath $pair[1]) {
            Write-Log "----- $($pair[0]) -----"
            try {
                Get-Content -LiteralPath $pair[1] -Encoding UTF8 | ForEach-Object { Write-Log $_ }
            } catch { Write-Log "(could not read $($pair[0]))" }
            Remove-Item -LiteralPath $pair[1] -ErrorAction SilentlyContinue
        }
    }
    Write-Log "finished_at: $((Get-Date).ToString('o'))"
    Write-Log "exit_code: $exitCode"
}
exit $exitCode
