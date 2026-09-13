<#
.SYNOPSIS
    Bootstrap audiobook-factory on Windows.

.DESCRIPTION
    Installs the host tools (uv, just, ffmpeg, Git for Windows) with scoop or
    winget, then builds the Python environments.

    Git for Windows matters as much as the rest: the justfile runs every recipe
    through bash. Without bash on PATH nothing in this project runs, even
    though the everyday scripts now have PowerShell twins.

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

# Cmd: what to look for on PATH. Scoop and winget name the same tools
# differently, and neither carries all four under one name.
$Tools = @(
    @{ Name = 'uv';     Cmd = 'uv';     Scoop = 'main/uv';     Winget = 'astral-sh.uv' },
    @{ Name = 'just';   Cmd = 'just';   Scoop = 'main/just';   Winget = 'casey.just' },
    @{ Name = 'ffmpeg'; Cmd = 'ffmpeg'; Scoop = 'main/ffmpeg'; Winget = 'Gyan.FFmpeg' },
    @{ Name = 'bash (Git for Windows)'; Cmd = 'bash'; Scoop = 'main/git'; Winget = 'Git.Git' }
)

Write-Bold "audiobook-factory bootstrap"
Write-Host "  host: Windows $([System.Environment]::OSVersion.Version), $env:PROCESSOR_ARCHITECTURE"

# scoop first: it installs per-user, needs no elevation, and is what this
# project's workstation uses.
$manager = ''
if (Get-Command scoop -ErrorAction SilentlyContinue)      { $manager = 'scoop' }
elseif (Get-Command winget -ErrorAction SilentlyContinue) { $manager = 'winget' }
Write-Host "  package manager: $(if ($manager) { $manager } else { 'none found' })"
Write-Host ""

Write-Bold "Checking host tools"
$Missing = @()
foreach ($tool in $Tools) {
    if (Get-Command $tool.Cmd -ErrorAction SilentlyContinue) {
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
    if (-not $manager) {
        Write-Fail "no scoop or winget found."
        Write-Host "  Install scoop:  irm get.scoop.sh | iex"
        Write-Host "  Or install 'App Installer' from the Microsoft Store for winget."
        exit 1
    }
    Write-Bold ("Installing: " + ($Missing.Name -join ', '))
    foreach ($tool in $Missing) {
        if ($manager -eq 'scoop') {
            Write-Host "  scoop install $($tool.Scoop)"
            scoop install $tool.Scoop
        } else {
            Write-Host "  winget install $($tool.Winget)"
            winget install --id $tool.Winget --accept-source-agreements --accept-package-agreements --silent
        }
    }
    # The installers update the machine PATH but not this session's copy of it.
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path', 'User')
    Write-Host ""
    foreach ($tool in $Missing) {
        if (Get-Command $tool.Cmd -ErrorAction SilentlyContinue) {
            Write-Ok $tool.Name
        } else {
            Write-Miss "$($tool.Name) still not on PATH; close this window and open a new one"
        }
    }
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
    Write-Bold "Building the Python environments"
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
    .\bin\audiobook.ps1 -Help        the one-command path
    docs\RUNBOOK.md                  everyday tasks

  The everyday scripts have PowerShell twins, so a bash prompt is optional:
  install.ps1, bin\audiobook.ps1 and scripts\preprocess.ps1 alongside
  install.sh, bin/audiobook and scripts/preprocess.sh. 'just' still invokes
  bash for every recipe, which is why Git for Windows is checked above.

  On this machine's RTX 5090, read docs\HANDOFF-GPU.md before installing GPU
  wheels: the gpu-torch recipe still points at CUDA 12.4, which has no kernels
  for Blackwell cards.
"@
