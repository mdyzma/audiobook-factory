# Stage 1a - clean a raw voice recording into XTTS-ready audio.
# Usage: scripts\preprocess.ps1 data\raw\voices\michal.mp3 michal
#
# The PowerShell twin of preprocess.sh. Same ffmpeg filter chain, same output
# path; keep the two in step.
#
# The first parameter is $Audio rather than the $Input the shell script uses:
# $Input is an automatic variable in PowerShell, holding the pipeline
# enumerator, and a parameter of that name shadows it.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Audio,
    [Parameter(Mandatory = $true, Position = 1)][string]$Name
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$outDir = Join-Path $root "data\processed\$Name"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$cleaned = Join-Path $outDir 'cleaned_full.wav'

# afftdn strips microphone hiss; loudnorm evens out level. Training on noisy
# audio teaches the clone to reproduce the noise, which is the usual amateur mistake.
ffmpeg -hide_banner -y -i $Audio `
    -af "afftdn=nf=-25,loudnorm=I=-19:TP=-2:LRA=9" `
    -ar 24000 -ac 1 -c:a pcm_s16le `
    $cleaned
if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed on $Audio" }

$duration = ffprobe -v error -show_entries format=duration -of csv=p=0 $cleaned
# InvariantCulture: ffprobe emits a decimal point, and parsing it under a locale
# such as pl-PL would otherwise read "123.4" as 1234.
$seconds = [double]::Parse($duration, [System.Globalization.CultureInfo]::InvariantCulture)
'cleaned -> {0} ({1:N1} min, 24kHz mono s16)' -f $cleaned, ($seconds / 60)
