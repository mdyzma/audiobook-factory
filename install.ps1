<#
.SYNOPSIS
    Bootstrap audiobook-factory on Windows.

.DESCRIPTION
    Installs the host tools (uv, just, ffmpeg, Git for Windows) with winget,
    then builds the three Python environments.

    Git for Windows matters as much as the rest: the justfile runs every recipe
    through bash, and bin/audiobook and scripts/preprocess.sh are bash scripts.
    Without bash on PATH nothing in this project runs.

.PARAMETER Check
    Report what is missing and change nothing.

.PARAMETER NoSetup
    Install the host tools but skip 'just setup'.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Check
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoSetup
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Write-Bold { param($Text) Write-Host $Text -ForegroundColor White }
function Write-Ok   { param($Text) Write-Host "  ok      $Text" -ForegroundColor Green }
function Write-Miss { param($Text) Write-Host "  missing $Text" -ForegroundColor Yellow }
function Write-Fail { param($Text) Write-Host "  failed  $Text" -ForegroundColor Red }

# id: what winget calls it. cmd: what to look for on PATH.
$Tools = @(
    @{ Name = 'uv';     Cmd = 'uv';     Id = 'astral-sh.uv' },
    @{ Name = 'just';   Cmd = 'just';   Id = 'casey.just' },
    @{ Name = 'ffmpeg'; Cmd = 'ffmpeg'; Id = 'Gyan.FFmpeg' },
    @{ Name = 'bash (Git for Windows)'; Cmd = 'bash'; Id = 'Git.Git' }
)

Write-Bold "audiobook-factory bootstrap"
Write-Host "  host: Windows $([System.Environment]::OSVersion.Version), $env:PROCESSOR_ARCHITECTURE"

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Fail "winget not found. Install 'App Installer' from the Microsoft Store, or install the tools by hand."
    exit 1
}
Write-Host ""

Write-Bold "Checking host tools"
$Missing = @()
foreach ($tool in $Tools) {
    $found = Get-Command $tool.Cmd -ErrorAction SilentlyContinue
    if ($found) {
        Write-Ok $tool.Name
    } else {
        Write-Miss $tool.Name
        $Missing += $tool
    }
}

if ($Missing.Count -eq 0) {
    Write-Host ""
    Write-Bold "All host tools present."
} else {
    Write-Host ""
    if ($Check) {
        Write-Bold ("Would install: " + ($Missing.Name -join ', '))
        exit 1
    }
    Write-Bold ("Installing: " + ($Missing.Name -join ', '))
    foreach ($tool in $Missing) {
        Write-Host "  winget install $($tool.Id)"
        winget install --id $tool.Id --accept-source-agreements --accept-package-agreements --silent
    }
    # winget updates the machine PATH but not this session's copy of it.
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path', 'User')
    Write-Host ""
    Write-Host "  note: if a tool is still not found, close this window and open a new one."
}

if ($Check) {
    Write-Host ""
    Write-Bold "Check only; nothing was changed."
    exit 0
}

# The bash scripts break with a stray carriage return if git rewrites line
# endings on checkout, and the failure message does not say so.
$autocrlf = (git config --get core.autocrlf 2>$null)
if ($autocrlf -eq 'true') {
    Write-Host ""
    Write-Bold "Line endings"
    Write-Host "  core.autocrlf=true rewrites the bash scripts with CRLF, which breaks them"
    Write-Host "  with an error that does not mention line endings."
    git config core.autocrlf input
    Write-Ok "set core.autocrlf=input for this repository"

    # Re-checking out is what actually fixes already-converted files, but it
    # discards uncommitted work, so only do it when there is none to lose.
    $dirty = (git status --porcelain)
    if ([string]::IsNullOrWhiteSpace($dirty)) {
        git rm --cached -r . --quiet
        git reset --hard --quiet
        Write-Ok "re-checked out with LF endings"
    } else {
        Write-Miss "working tree has uncommitted changes, so files were left as they are"
        Write-Host "  Commit or stash, then run:  git rm --cached -r . ; git reset --hard"
    }
}

if (-not $NoSetup) {
    Write-Host ""
    Write-Bold "Building the three Python environments"
    Write-Host "  This downloads roughly 3 GB and takes a few minutes."
    Write-Host "  uv installs its own Python 3.11.9; nothing is compiled."
    Write-Host ""
    just setup
    Write-Host ""
    just doctor
}

Write-Host ""
Write-Bold "Done."
Write-Host @"
  Next:
    just doctor                      confirm the environments resolved
    bash bin/audiobook --help        the one-command path (needs bash)
    docs\RUNBOOK.md                  everyday tasks

  Run the pipeline from Git Bash rather than PowerShell: bin/audiobook and
  scripts/preprocess.sh are bash scripts. 'just' works from either, because
  the justfile invokes bash for every recipe.

  On this machine's RTX 5090, read docs\HANDOFF-GPU.md before installing GPU
  wheels: the gpu-torch recipe still points at CUDA 12.4, which has no kernels
  for Blackwell cards.
"@
