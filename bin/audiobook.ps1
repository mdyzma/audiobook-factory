# audiobook - clone a voice from a sample and read an ebook in it.
#
# The PowerShell twin of bin/audiobook. Same stages in the same order, same
# defaults; keep the two in step. Options are PowerShell-shaped (-Voice, -Book)
# rather than the shell script's -v/--voice, because a script that accepts
# --voice under PowerShell is fighting the parser for no benefit.
#
#   .\bin\audiobook.ps1 -Voice C:\Users\me\voice.mp3 -Book C:\Books\solaris.epub
[CmdletBinding()]
param(
    # Recording to clone. mp3, wav, m4a, anything ffmpeg reads.
    [string]$Voice,
    # Ebook to narrate. .epub, .pdf or .txt.
    [string]$Book,
    # A directory, or a full filename whose extension picks the format
    # (.m4b default, .mp3, .wav). Defaults to the Downloads folder.
    [string]$Output,
    # Language of both the recording and the book.
    [string]$Language = 'pl',
    # Name for the cloned voice. Default: the sample's filename.
    [string]$Name,
    # Working name for the book. Default: the ebook's filename.
    [string]$Slug,
    # Render silence instead of speech. Checks the whole structure in seconds.
    [switch]$DryRun,
    # Re-clone the voice even if it already exists.
    [switch]$Reclone,
    # Re-transcribe the result and report word error rate.
    [switch]$Verify,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Die($message) { Write-Host "audiobook: $message" -ForegroundColor Red; exit 1 }
function Step($message) { Write-Host ''; Write-Host "==> $message" -ForegroundColor White }

if ($Help -or (-not $Voice -and -not $Book)) {
    @'
audiobook - clone a voice from a sample and read an ebook in it

USAGE
    .\bin\audiobook.ps1 -Voice SAMPLE -Book EBOOK [-Output PATH] [options]

REQUIRED
    -Voice SAMPLE     Recording to clone. mp3, wav, m4a, anything ffmpeg reads.
    -Book EBOOK       Ebook to narrate. .epub, .pdf or .txt.

OPTIONAL
    -Output PATH      Where to put the audiobook. A directory or a full
                      filename. Defaults to your Downloads folder.
                      The extension picks the format: .m4b (default), .mp3, .wav.
    -Language CODE    Language of both the recording and the book. Default: pl.
    -Name NAME        Name for the cloned voice. Default: the sample's filename.
    -Slug SLUG        Working name for the book. Default: the ebook's filename.
    -DryRun           Render silence instead of speech. Checks the whole
                      structure in seconds without loading a model.
    -Reclone          Re-clone the voice even if it already exists.
    -Verify           Re-transcribe the result and report word error rate.
    -Help             This message.

EXAMPLES
    .\bin\audiobook.ps1 -Voice ~\Desktop\my-voice.mp3 -Book ~\Books\solaris.epub
    .\bin\audiobook.ps1 -Voice my-voice.mp3 -Book solaris.epub -Output ~\Music\solaris.mp3
    .\bin\audiobook.ps1 -Voice my-voice.mp3 -Book solaris.epub -DryRun

NOTES
    On the GPU this is far faster than the 0.4x realtime the Mac manages, but
    the run is resumable either way: interrupt it and run the same command again.

    config\cast.yml decides which voice reads which role. If it names a single
    voice, this command repoints it at -Voice for you. If it already casts two
    or more voices it is left alone, since that encodes a decision about who
    reads what; add the new voice to it by hand.

    A voice is cloned once. Later runs with the same -Name reuse it.
'@
    exit 0
}

if (-not $Voice) { Die 'missing -Voice' }
if (-not $Book)  { Die 'missing -Book' }
if (-not (Test-Path -PathType Leaf $Voice)) { Die "no such file: $Voice" }
if (-not (Test-Path -PathType Leaf $Book))  { Die "no such file: $Book" }

foreach ($tool in 'just', 'ffmpeg') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Die "$tool is not installed (scoop install $tool). Run .\install.ps1 first."
    }
}
# Every just recipe runs under Git for Windows' bash, named by path in the
# justfile, so its absence fails later and obscurely. `bash` on PATH proves
# nothing: System32\bash.exe is the WSL launcher.
if (-not (Test-Path -PathType Leaf 'C:\Program Files\Git\bin\bash.exe')) {
    Die 'Git for Windows is not installed at C:\Program Files\Git, and every just recipe runs under its bash (winget install Git.Git)'
}

# Turn a path into a safe identifier: strip directories, extension and anything
# that would be awkward in a filename.
function ConvertTo-Slug($path) {
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($path).ToLowerInvariant()
    $stem = $stem -replace '\s', '-'
    $stem = $stem -replace '[^a-z0-9._-]', ''
    return $stem.Trim('-')
}

if (-not $Name) { $Name = ConvertTo-Slug $Voice }
if (-not $Slug) { $Slug = ConvertTo-Slug $Book }
if (-not $Name) { Die 'could not derive a voice name; pass -Name' }
if (-not $Slug) { Die 'could not derive a book name; pass -Slug' }

# Output: a directory, or a filename whose extension picks the format.
if (-not $Output) { $Output = Join-Path $HOME 'Downloads' }
if ((Test-Path -PathType Container $Output) -or $Output.EndsWith('\') -or $Output.EndsWith('/')) {
    $format = 'm4b'
    $outDir = $Output.TrimEnd('\', '/')
    $outFile = Join-Path $outDir "$Slug.$format"
} else {
    $outDir = Split-Path -Parent $Output
    if (-not $outDir) { $outDir = '.' }
    $outFile = $Output
    $format = [System.IO.Path]::GetExtension($Output).TrimStart('.').ToLowerInvariant()
    if (-not $format) { Die "no extension on -Output '$Output'; use .m4b, .mp3 or .wav" }
}
if ($format -notin 'm4b', 'mp3', 'wav') { Die "unsupported output format '.$format'; use .m4b, .mp3 or .wav" }
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

Write-Host "voice sample : $Voice  (as '$Name')"
Write-Host "ebook        : $Book  (as '$Slug')"
Write-Host "output       : $outFile"
Write-Host "language     : $Language"
if ($DryRun) { Write-Host 'mode         : dry run, silence instead of speech' }

function Invoke-Just {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & just @Arguments
    if ($LASTEXITCODE -ne 0) { Die "just $($Arguments -join ' ') failed" }
}

# --- stage 1: the voice. Cloning is once per voice, not once per book. -------
if ($Reclone -or -not (Test-Path "data\voices\$Name.json")) {
    Step 'Cleaning the recording'
    Invoke-Just clean $Voice $Name
    Step 'Labelling it (WhisperX; the model downloads once)'
    Invoke-Just label $Name auto $Language
    Step 'Cloning the voice'
    Invoke-Just clone $Name
    Write-Host "audition: data\voices\$Name\audition.wav"
} else {
    Step "Voice '$Name' already cloned; reusing it (-Reclone to redo)"
}

# Point the cast at this voice, but never flatten a cast someone built by hand.
#
# The convenience only makes sense for a single-voice cast, where rewriting the
# one entry is obviously what was meant. Once the cast names two or more voices
# it encodes a decision about who reads what, and silently collapsing that to
# one voice would be destructive and hard to notice.
$castPath = 'config\cast.yml'
$castLines = @()
if (Test-Path $castPath) { $castLines = Get-Content $castPath }
$castVoices = @($castLines |
    ForEach-Object { if ($_ -match '^\s+voice:\s*([A-Za-z0-9._-]+)\s*$') { $Matches[1] } } |
    Sort-Object -Unique)

if ($castVoices.Count -gt 1) {
    Step "Leaving $castPath alone: it already casts $($castVoices.Count) voices"
    $castVoices | ForEach-Object { Write-Host "    $_" }
    if ($castVoices -notcontains $Name) {
        Write-Host "    note: '$Name' is not in that cast, so it will not be used." -ForegroundColor Yellow
        Write-Host "    Add it to $castPath, or pass -Name to match an existing role." -ForegroundColor Yellow
    }
} elseif ($castVoices -notcontains $Name) {
    Step "Pointing the cast at '$Name'"
    $updated = $castLines | ForEach-Object {
        $_ -replace '^(\s+voice:)\s*[A-Za-z0-9._-]+\s*$', "`$1 $Name"
    }
    # WriteAllLines with an explicit encoding: Set-Content under Windows
    # PowerShell would write UTF-16 or a BOM, and the YAML is read by Python.
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines((Resolve-Path $castPath).Path, $updated, $utf8)
}

# --- stages 2-5: the book ----------------------------------------------------
Step 'Reading the ebook'
Invoke-Just ingest $Book $Slug $Language

Step 'Splitting it into fragments'
Invoke-Just chunk $Slug

if ($DryRun) {
    Step 'Rendering silence'
    Invoke-Just dryrun $Slug
} else {
    Step 'Synthesising (resumable: re-run this command to continue)'
    Invoke-Just synth $Slug
}

Step 'Assembling'
Invoke-Just assemble $Slug $format

if ($Verify -and -not $DryRun) {
    Step 'Checking the result against the source text'
    # Deliberately not fatal: a poor word error rate is a finding, not a failure.
    & just verify $Slug
}

Push-Location 'apps\studio'
try {
    $result = (& uv run python -m studio.catalog_cli output $Slug --fmt $format |
        Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0) { Die 'could not locate the assembled output' }
    if (-not $result) { Die 'the catalog named no output file' }
} finally {
    Pop-Location
}
Copy-Item -LiteralPath $result -Destination $outFile -Force
Write-Host ''
Write-Host "Done. $outFile" -ForegroundColor Green
