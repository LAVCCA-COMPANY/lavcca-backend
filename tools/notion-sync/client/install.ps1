# Team PC setup: docs (obsidian-vault/Notion only) -> <existing vault>\LAVCCA, at logon and every minute.
# usage (PowerShell, own GitHub login):
#   .\install.ps1 -Vault "$env:OneDrive\문서\Obsidian Vault" -DryRun   clone + show what the first sync would change
#   .\install.ps1 -Vault "$env:OneDrive\문서\Obsidian Vault"           register the task (re-run to update script/settings)
#   .\install.ps1 -Status                                             task result, last sync, last error, log tail
#   .\install.ps1 -Login                                              GitHub sign-in again (after "sign-in needed" errors)
#   .\install.ps1 -Repair                                             set aside a broken docs copy and clone it again
#   .\install.ps1 -Uninstall                                          remove the task (LAVCCA\ and backups stay)
# Nothing outside <vault>\LAVCCA is written. No tokens are stored by this script: Git Credential Manager keeps the login.
param(
    [string]$Vault,
    [switch]$DryRun,
    [switch]$Status,
    [switch]$Login,
    [switch]$Repair,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$State = Join-Path $env:LOCALAPPDATA "LAVCCA"
$Repo = Join-Path $State "docs"
$Script = Join-Path $State "obsidian_pull.py"
$Log = Join-Path $State "logs\pull.log"
$TaskName = "LAVCCA Notion to Obsidian"

function Get-Python {
    $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $pyw) { throw "Python 3 not found: winget install Python.Python.3.12" }
    return @{ w = $pyw; c = ($pyw -replace "pythonw.exe$", "python.exe") }
}

function Clone-Docs {
    # partial clone of the private docs repo with the member's own GitHub login (Git Credential Manager);
    # autocrlf off: the vault gets the published bytes, not Windows line endings
    git clone -c core.autocrlf=false --filter=blob:none --sparse https://github.com/LAVCCA-COMPANY/docs.git $Repo
    if ($LASTEXITCODE) { throw "git clone failed (GitHub sign-in or read access to LAVCCA-COMPANY/docs?)" }
    git -C $Repo sparse-checkout set obsidian-vault/Notion
    if ($LASTEXITCODE) { throw "git sparse-checkout failed" }
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "task removed. Kept: <vault>\LAVCCA, $State (docs copy, trash, logs). Delete them by hand if no longer wanted."
    exit 0
}
if ($Status) {
    $py = Get-Python
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($info) { Write-Host ("task           : last run {0}, result {1}, next {2}" -f $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime) }
    else { Write-Host "task           : NOT registered" }
    $v = if ($Vault) { $Vault } else { (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).Actions[0].Arguments -replace '.*--vault "([^"]+)".*', '$1' }
    & $py.c $Script --status --vault $v
    $code = $LASTEXITCODE
    if (Test-Path $Log) { Write-Host "--- log (last 5)"; Get-Content $Log -Tail 5 }
    exit $code
}
if ($Login) {
    $py = Get-Python
    & $py.c $Script --login
    exit $LASTEXITCODE
}
if ($Repair) {
    if (Test-Path $Repo) {
        $aside = "$Repo.broken-$(Get-Date -Format yyyyMMddHHmmss)"
        Move-Item $Repo $aside   # set aside, never deleted or reset
        Write-Host "old docs copy moved to $aside"
    }
    Clone-Docs
    Write-Host "docs copy re-created. The next run (within a minute) re-checks LAVCCA in full."
    exit 0
}

if (-not $Vault) { throw "-Vault is required, e.g. -Vault `"$env:OneDrive\문서\Obsidian Vault`"" }
if (-not (Test-Path (Join-Path $Vault ".obsidian"))) { throw "not an Obsidian vault (no .obsidian): $Vault" }
$py = Get-Python
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw "Git not found: winget install Git.Git" }

New-Item -ItemType Directory -Force -Path $State | Out-Null
if (-not (Test-Path (Join-Path $Repo ".git"))) { Clone-Docs }
Copy-Item -Force (Join-Path $PSScriptRoot "obsidian_pull.py") $Script

if ($DryRun) {
    & $py.c $Script --vault $Vault
    exit $LASTEXITCODE
}

$action = New-ScheduledTaskAction -Execute $py.w -Argument "`"$Script`" --vault `"$Vault`" --apply --log `"$Log`""
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT1M"  # network and OneDrive first
# Every minute: a run with nothing new is one `git ls-remote` (no REST API quota) and a stat of LAVCCA\.
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logon, $every -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "registered '$TaskName' (logon + every minute, also on battery). log: $Log"
Write-Host "check: .\install.ps1 -Status"
