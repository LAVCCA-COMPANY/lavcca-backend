# Team PC setup: docs (obsidian-vault/Notion only) -> <existing vault>\LAVCCA, at logon and every 15 minutes.
# usage (PowerShell, own GitHub login):  .\install.ps1 -Vault "$env:OneDrive\문서\Obsidian Vault"
#   -DryRun   only clone and print what the first sync would change (registers nothing, writes nothing to the vault)
# Nothing outside <vault>\LAVCCA is written. Remove: Unregister-ScheduledTask "LAVCCA Notion to Obsidian"
param(
    [Parameter(Mandatory = $true)][string]$Vault,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$State = Join-Path $env:LOCALAPPDATA "LAVCCA"
$Repo = Join-Path $State "docs"
$Script = Join-Path $State "obsidian_pull.py"
$TaskName = "LAVCCA Notion to Obsidian"

if (-not (Test-Path (Join-Path $Vault ".obsidian"))) { throw "not an Obsidian vault (no .obsidian): $Vault" }
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python 3 not found: winget install Python.Python.3.12" }
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) { throw "Git not found: winget install Git.Git" }

New-Item -ItemType Directory -Force -Path $State | Out-Null
if (-not (Test-Path (Join-Path $Repo ".git"))) {
    # partial clone of the private docs repo with the member's own GitHub login (Git Credential Manager)
    # autocrlf off: the vault gets the published bytes, not Windows line endings
    git clone -c core.autocrlf=false --filter=blob:none --sparse https://github.com/LAVCCA-COMPANY/docs.git $Repo
    if ($LASTEXITCODE) { throw "git clone failed" }
    git -C $Repo sparse-checkout set obsidian-vault/Notion
    if ($LASTEXITCODE) { throw "git sparse-checkout failed" }
}
Copy-Item -Force (Join-Path $PSScriptRoot "obsidian_pull.py") $Script

$py = $python -replace "pythonw.exe$", "python.exe"
if ($DryRun) {
    & $py $Script --vault $Vault
    exit $LASTEXITCODE
}

$log = Join-Path $State "logs\pull.log"
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$Script`" --vault `"$Vault`" --apply --log `"$log`""
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT2M"  # network and OneDrive first
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logon, $every -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "registered '$TaskName' (logon + every 15 min). log: $log"
