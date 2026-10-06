#Requires -Version 7
<#
Build the packaged Windows client (dist\MeetingNotes\ and MeetingNotes-Windows.zip).

Reproduces the build the release engineer runs by hand on the Windows build PC:

    cd <repo>; pwsh tools/build_windows.ps1

Uses the build venv .build-tools\build-venv-313 (Python 3.13, client + Nuitka only,
so the server stack stays out of the app). That venv has no pip; Nuitka compiles
meeting_notes straight from the repo (the script directory wins on sys.path), so
nothing needs installing. If pip is ever present it refreshes the package first.
Produces:

    <repo>\dist\MeetingNotes\MeetingNotes.exe
    <OutDir>\MeetingNotes-Windows.zip     (upload to <data>/client/)

Parameters: -OutDir (where the zip goes, default the repo root), -GitHubRelease
(also run `gh release create v<version>` with the zip; needs `gh` logged in).
#>
[CmdletBinding()]
param(
    [string]$OutDir,
    [switch]$GitHubRelease
)
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = $Root }
$Venv = Join-Path $Root '.build-tools\build-venv-313'
$Py = Join-Path $Venv 'Scripts\python.exe'
$Dist = Join-Path $Root 'dist'
$Zip = Join-Path $OutDir 'MeetingNotes-Windows.zip'
# Keep Nuitka's downloads/caches with the other build tools when they exist.
$Cache = Join-Path $Root '.build-tools\nuitka-cache-313'
if (Test-Path $Cache) { $env:NUITKA_CACHE_DIR = $Cache }

function Step($Message) { Write-Host "`n==> $Message" }
function Fail($Message) { throw $Message }

if (-not (Test-Path $Py)) { Fail "Build venv not found: $Py" }

# The version comes from the source file (not imported), same as the server reports it.
$Match = Select-String -LiteralPath (Join-Path $Root 'meeting_notes\__init__.py') `
    -Pattern '^__version__\s*=\s*"([^"]+)"'
if (-not $Match) { Fail 'Could not read __version__ from meeting_notes/__init__.py' }
$Version = $Match.Matches[0].Groups[1].Value
Write-Host "Meeting Notes version $Version"

Push-Location $Root
try {
    Step 'Dependencies'
    & $Py -m pip --version *> $null
    if ($LASTEXITCODE -eq 0) {
        & $Py -m pip install --no-deps --force-reinstall .
        if ($LASTEXITCODE -ne 0) { Fail "pip install failed with code $LASTEXITCODE" }
    } else {
        Write-Host 'The build venv has no pip; Nuitka compiles meeting_notes from the repo.'
    }

    Step 'Compiling with Nuitka (takes a while)'
    if (Test-Path $Dist) { Remove-Item -LiteralPath $Dist -Recurse -Force }
    $Nuitka = @(
        '-m', 'nuitka', '--standalone', '--windows-console-mode=disable',
        '--enable-plugin=pyside6', '--include-package=meeting_notes',
        '--include-package=soundcard',
        '--include-package-data=meeting_notes',
        '--include-package-data=soundcard',
        '--windows-icon-from-ico=assets/meeting-notes.ico',
        '--output-dir=dist', '--output-filename=MeetingNotes.exe',
        '--assume-yes-for-downloads',
        'windows_client.py'
    )
    & $Py @Nuitka
    if ($LASTEXITCODE -ne 0) { Fail "Nuitka failed with code $LASTEXITCODE" }

    Step 'Normalize package directory'
    Move-Item -LiteralPath (Join-Path $Dist 'windows_client.dist') -Destination (Join-Path $Dist 'MeetingNotes')

    Step 'Smoke test packaged client'
    $Process = Start-Process -FilePath (Join-Path $Dist 'MeetingNotes\MeetingNotes.exe') `
        -ArgumentList '--smoke-test' -PassThru -Wait
    if ($Process.ExitCode -ne 0) {
        Fail "Packaged client self-test failed with code $($Process.ExitCode)"
    }
    Write-Host 'smoke test passed'

    Step 'Zip'
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    if (Test-Path $Zip) { Remove-Item -LiteralPath $Zip -Force }
    Compress-Archive -Path (Join-Path $Dist 'MeetingNotes\*') -DestinationPath $Zip
    Get-Item $Zip | Format-List Name, Length
    Get-FileHash -Algorithm SHA256 $Zip | Format-List Hash

    if ($GitHubRelease) {
        Step "GitHub release v$Version"
        gh release create "v$Version" $Zip --generate-notes
        if ($LASTEXITCODE -ne 0) { Fail "gh release create failed with code $LASTEXITCODE" }
    }
}
finally {
    Pop-Location
}

Write-Host "`nDone. Upload $Zip to <server data>/client/MeetingNotes-Windows.zip"
