"""The Install page and the client installer / uninstaller scripts."""

from __future__ import annotations

import html
import json

from .shared import _JS_HELPERS
from .shell import _icon, _shell


def render_install_page(
    server_address: str,
    *,
    token_configured: bool,
    appearance: str = "system",
    macos_package_available: bool = True,
) -> str:
    """Human-readable Windows, macOS and phone installation and first-run guide.

    One tab per device across the top (Windows, Mac, Phone); only the chosen device's instructions show. The page
    opens on the tab for the visitor's own device, or the one a hash names (``#macos``, ``#macos-install``, ...).

    ``macos_package_available`` is whether ``MeetingNotes-macOS.zip`` is published on this
    server; when it is not, the Mac download button is shown disabled with a note.
    """
    address = html.escape(server_address.rstrip("/"))
    copy_icon = _icon("copy")
    if macos_package_available:
        mac_download_button = '<a class="btn primary" href="/install/MeetingNotes-macOS.zip" download>Download the Mac app (.zip)</a>'
        mac_download_note = ""
    else:
        mac_download_button = '<button type="button" class="btn primary" disabled>Download the Mac app (.zip)</button>'
        mac_download_note = (
            '<p class="help" id="mac-package-missing">The Mac app has not been published on this server yet, '
            "so there is nothing to download. Ask the server's operator to upload it.</p>"
        )
    body = (
        fr"""
<nav class="crumbs" aria-label="Breadcrumb"><a href="/settings#settings-install-heading">Settings</a><span aria-hidden="true">/</span><span aria-current="page">Install</span></nav>
<div class="page-head"><h1>Install Meeting Notes</h1></div>
<div class="doc">
<div class="tabs install-tabs" role="tablist" aria-label="Device">
  <button type="button" role="tab" id="tab-windows" aria-controls="windows" aria-selected="true" class="active">Windows</button>
  <button type="button" role="tab" id="tab-macos" aria-controls="macos" aria-selected="false" tabindex="-1">Mac</button>
  <button type="button" role="tab" id="tab-phone" aria-controls="phone" aria-selected="false" tabindex="-1">Phone</button>
</div>
<section id="windows" role="tabpanel" aria-labelledby="tab-windows" tabindex="-1">
  <h2>Windows</h2>
  <p class="help">Jump to: <a href="#windows-install">Install</a> &middot; <a href="#windows-download">Download only</a> &middot; <a href="#windows-update">Update</a> &middot; <a href="#windows-uninstall">Uninstall</a> &middot; <a href="#windows-troubleshooting">Troubleshooting</a></p>
  <h3>What the installer includes</h3>
  <p>The Windows package is self-contained. It includes Python, Qt, NumPy,
  SoundCard, the HTTP client, WebSocket support, and their native runtime files.
  You do not need Python, pip, a compiler, an audio driver, or administrator access.</p>
  <p class="help">Requirements: 64-bit Windows 10 or 11, PowerShell 5.1 or newer,
  and access to <strong>{address}</strong> on your LAN.</p>
  <h3 id="windows-install">Install</h3>
  <p><strong>Fastest option: run this one-step command in PowerShell.</strong> It downloads the installer directly from this server and runs it for the current Windows user.</p>
  <pre class="command" id="cmd-oneline" tabindex="0">powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm '{address}/install/client-agent.ps1' | iex"</pre>
  <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="cmd-oneline">{copy_icon}<span>Copy command</span></button></div>
  <h3 id="windows-download">Download only</h3>
  <ol>
    <li><a class="btn primary" href="/install/client-agent.ps1" download>Download installer</a></li>
    <li>Open PowerShell normally. Administrator mode is not required.</li>
    <li>Run the downloaded script:</li>
  </ol>
  <pre class="command" id="cmd-file" tabindex="0">powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-MeetingNotes.ps1"</pre>
  <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="cmd-file">{copy_icon}<span>Copy command</span></button></div>
  <h3 id="windows-update">Update</h3>
  <p class="help">If your browser renamed the file, use its actual filename. Re-running
  the installer upgrades the application and preserves your existing server token,
  recording folder, and client settings.</p>
  <h3 id="windows-uninstall">Uninstall</h3>
  <p><a class="btn secondary" href="/install/uninstall-client.ps1" download>Download uninstaller</a></p>
  <p class="help">Run it in normal PowerShell to remove the per-user application and shortcuts.
  Recordings and <code>%USERPROFILE%\.meeting-notes</code> settings are preserved by default.
  Add <code>-RemoveSettings</code> only when you also want to remove client settings.</p>
  <h3>First run</h3>
  <ol>
    <li>Launch <strong>Meeting Notes</strong> from the Start Menu or desktop shortcut.</li>
    <li>Open <strong>Settings</strong>. The Server URL should already be <strong>{address}</strong>.</li>
    <li>Enter the same server token used to sign in to this website, then save.</li>
    <li>Confirm the microphone and system-audio devices shown in the main window.</li>
    <li>Enter an optional meeting name and select <strong>Start recording</strong>.</li>
    <li>Select <strong>Stop recording</strong> when finished. Upload and transcription happen automatically.</li>
  </ol>
  <p>The installer also writes <code>How to run Meeting Notes.txt</code> into the
  application folder at <code>%LOCALAPPDATA%\MeetingNotes</code>.</p>
  <h3 id="windows-troubleshooting">Troubleshooting</h3>
  <ul>
    <li>If Windows blocks the download, keep the file only if it came from this server page.</li>
    <li>If the app says the server rejected the token, copy the web-login token again in Settings.</li>
    <li>Remote Desktop may not expose a microphone. Test once from the physical Windows session.</li>
    <li>The installer checks the server before launching and prints a warning if the LAN address is unavailable.</li>
    <li>To send diagnostics for troubleshooting, open <strong>Logs</strong> in the app and choose <strong>Send to server</strong>. Uploads are listed under Settings, Client logs.</li>
    <li>The installer and uninstaller never delete recordings. If your recordings folder sits inside the app folder they stop with a message and change nothing.</li>
  </ul>
</section>
<section id="macos" role="tabpanel" aria-labelledby="tab-macos" tabindex="-1" hidden>
  <h2>macOS</h2>
  <p class="help">Jump to: <a href="#macos-install">Install</a> &middot; <a href="#macos-download">Download only</a> &middot; <a href="#macos-update">Update</a> &middot; <a href="#macos-uninstall">Uninstall</a> &middot; <a href="#macos-troubleshooting">Troubleshooting</a></p>
  <p>For Apple silicon Macs running macOS 13 or newer.</p>
  <h3 id="macos-install">Install</h3>
  <p><strong>Fastest option: run this one-step command in Terminal.</strong> No administrator password is needed.</p>
  <pre class="command" id="cmd-mac" tabindex="0">curl -fsSL {address}/install/mac.sh | bash</pre>
  <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="cmd-mac">{copy_icon}<span>Copy command</span></button></div>
  <p class="help">The script verifies the download against this server, installs
  <strong>Meeting Notes</strong> into <code>~/Applications</code>, keeps your existing settings and
  recordings, points the app at <strong>{address}</strong>, and opens it.
  No BlackHole or other audio driver is needed: system audio is captured with macOS itself.</p>
  <h3 id="macos-download">Download only</h3>
  <ol>
    <li>{mac_download_button}</li>
    <li>Unzip it, then move <strong>Meeting Notes.app</strong> to <code>~/Applications</code> (or <code>/Applications</code>).</li>
    <li>The app is ad-hoc signed, so the first time <strong>right-click it and choose Open</strong>, then confirm. Or remove the quarantine flag in Terminal and open it normally:</li>
  </ol>
  <pre class="command" id="cmd-mac-quarantine" tabindex="0">xattr -dr com.apple.quarantine "$HOME/Applications/Meeting Notes.app"</pre>
  <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="cmd-mac-quarantine">{copy_icon}<span>Copy command</span></button></div>
  <ol start="4">
    <li>Allow <strong>Microphone</strong> and <strong>Screen &amp; System Audio Recording</strong> when macOS asks.</li>
    <li>Open <strong>Settings</strong> in the app and enter the server address <strong>{address}</strong> and the server token.</li>
  </ol>
  {mac_download_note}
  <p><strong>First run on a Mac</strong></p>
  <ol>
    <li>When macOS asks, allow <strong>Microphone</strong> access.</li>
    <li>Start a recording once. macOS will ask for <strong>Screen &amp; System Audio Recording</strong>: open
    <em>System Settings &rarr; Privacy &amp; Security &rarr; Screen &amp; System Audio Recording</em>, switch
    <strong>Meeting Notes</strong> on, then quit and reopen the app. Only audio is captured, never the screen.</li>
    <li>Open <strong>Settings</strong> in the app and enter the same server token used to sign in here.</li>
    <li>In <em>Privacy &amp; Security &rarr; Microphone</em> and <em>Screen &amp; System Audio Recording</em>, confirm Meeting Notes is switched on if a track stays silent.</li>
  </ol>
  <h3 id="macos-update">Update</h3>
  <p class="help">Run the install command again any time to update. It keeps your settings, token and
  recordings. The app's own <strong>Update available</strong> button does the same.
  The app is ad-hoc signed, so macOS may ask for the Microphone and Screen Recording permissions again after an update.</p>
  <h3 id="macos-uninstall">Uninstall</h3>
  <p><strong>Run this in Terminal.</strong> It quits the app and removes it and its settings.</p>
  <pre class="command" id="cmd-mac-uninstall" tabindex="0">curl -fsSL {address}/install/uninstall-mac.sh | bash</pre>
  <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="cmd-mac-uninstall">{copy_icon}<span>Copy command</span></button></div>
  <p><a class="btn secondary" href="/install/uninstall-mac.sh" download="uninstall-mac.sh">Download uninstaller</a></p>
  <p class="help">If you downloaded it, run <code>bash ~/Downloads/uninstall-mac.sh</code> in Terminal.
  Recordings (<code>~/Meeting Notes</code> or your chosen folder) are never deleted.
  Settings in <code>~/.meeting-notes</code> are removed; add <code>--keep-settings</code>
  (<code>bash -s -- --keep-settings</code> after the pipe) to keep them. It also clears the app's Microphone and Screen
  Recording permission entries. To remove by hand, drag <code>~/Applications/Meeting Notes.app</code> to the Trash.</p>
  <h3 id="macos-troubleshooting">Troubleshooting</h3>
  <ul>
    <li>If macOS says the app cannot be opened because it is from an unidentified developer, right-click it, choose <strong>Open</strong>, and confirm.</li>
    <li>If a track stays silent, check <em>System Settings &rarr; Privacy &amp; Security</em>: Meeting Notes must be on under Microphone and Screen &amp; System Audio Recording.</li>
    <li>To send diagnostics, open <strong>Logs</strong> in the app and choose <strong>Send to server</strong>. The installer's own log is <code>~/.meeting-notes/logs/update.log</code>.</li>
    <li>The installer and uninstaller never delete recordings. If your recordings folder sits inside the app (or, for the uninstaller, a folder it would remove) they stop with a message and change nothing.</li>
  </ul>
</section>
<section id="phone" role="tabpanel" aria-labelledby="tab-phone" tabindex="-1" hidden>
  <h2>Phone</h2>
  <p>For in-person meetings, an iPhone (Safari) or Android phone (Chrome) records with the phone recorder. Nothing
  to download: it is a web page on this server.</p>
  <h3 id="phone-open">Open it</h3>
  <ol>
    <li>On the phone, open <a href="/record/"><strong>{address}/record/</strong></a>. The microphone only works over HTTPS.</li>
    <li>Sign in with the same server token used to sign in to this website.</li>
    <li>Tap <strong>Record</strong>, and allow the microphone when the phone asks.</li>
  </ol>
  <h3 id="phone-install">Add it to the home screen</h3>
  <ul>
    <li><strong>iPhone:</strong> in Safari, tap Share, then <strong>Add to Home Screen</strong>.</li>
    <li><strong>Android:</strong> in Chrome, open the menu, then <strong>Install app</strong>.</li>
  </ul>
  <p class="help">Once it has been opened online, it opens and records with no connection and uploads later.</p>
  <h3 id="phone-recording">While recording</h3>
  <ul>
    <li>The page keeps the screen awake. On iPhone, locking the screen or switching apps pauses the recording; it
    starts again when the page is back (or shows <strong>Resume recording</strong>), and the gap is kept.</li>
    <li>Recordings not yet uploaded are listed under <strong>On this phone</strong>, with Upload and Retry.</li>
    <li>Speakers are labelled Speaker 1, Speaker 2 and so on; name them on the meeting page.</li>
  </ul>
</section>
</div>
<script>
"""
        + _JS_HELPERS
        + r"""
// Device tabs: one panel shows. A hash picks the tab (#macos, #macos-install, #phone-open...), else the visitor's
// own device (Mac, phone, else Windows). Arrow keys move between tabs.
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.install-tabs [role="tab"]'));
  function show(id, focusTab) {
    tabs.forEach(function (tab) {
      var on = tab.getAttribute('aria-controls') === id;
      tab.classList.toggle('active', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.tabIndex = on ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !on;
      if (on && focusTab) tab.focus();
    });
  }
  function fromHash() {
    var h = (location.hash || '').slice(1);
    if (!h) return null;
    var el = document.getElementById(h), panel = el && el.closest('[role="tabpanel"]');
    return panel ? panel.id : null;
  }
  function ownDevice() {
    var ua = navigator.userAgent || '';
    if (/iPhone|iPad|iPod|Android/i.test(ua)) return 'phone';
    if (/Macintosh|Mac OS X/i.test(ua) && !(navigator.maxTouchPoints > 1)) return 'macos';
    if (/Macintosh/i.test(ua)) return 'phone';  // an iPad reports itself as a Mac
    return 'windows';
  }
  function open() {
    var id = fromHash();
    show(id || ownDevice(), false);
    if (id && location.hash.length > 1) {
      var target = document.getElementById(location.hash.slice(1));
      if (target) target.scrollIntoView();
    }
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener('click', function () {
      show(tab.getAttribute('aria-controls'), false);
      history.replaceState(null, '', '#' + tab.getAttribute('aria-controls'));
    });
    tab.addEventListener('keydown', function (e) {
      var next = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : null;
      if (next === null) return;
      e.preventDefault();
      var t = tabs[(next + tabs.length) % tabs.length];
      show(t.getAttribute('aria-controls'), true);
      history.replaceState(null, '', '#' + t.getAttribute('aria-controls'));
    });
  });
  window.addEventListener('hashchange', open);
  open();
})();
document.querySelectorAll('[data-copy-target]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var target = document.getElementById(btn.dataset.copyTarget);
    if (!target) return;
    copyText(target.textContent).then(function (ok) { notify(ok ? 'Command copied.' : 'Could not copy. Select the command and copy it by hand.', ok ? '' : 'error'); });
  });
});
</script>
"""
    )
    return _shell(
        "Install", body, token_configured=token_configured, active="install", appearance=appearance
    )


# PowerShell shared by the installer and the uninstaller. Both scripts define
# $installDir before this runs. __ACTION__ is "installer" or "uninstaller".
_RECORDINGS_GUARD_PS = r'''
# --- Recordings safety guard -------------------------------------------------
# Runs first: before any process is stopped and before any file is touched.
# Replacing or removing the app folder must never take recordings with it.
function Get-NormalizedPath([string]$path) {
    return [IO.Path]::GetFullPath($path).TrimEnd('\', '/')
}

function Test-PathInside([string]$path, [string]$folder) {
    $candidate = Get-NormalizedPath $path
    $container = Get-NormalizedPath $folder
    return ($candidate.Equals($container, [StringComparison]::OrdinalIgnoreCase) -or
        $candidate.StartsWith($container + "\", [StringComparison]::OrdinalIgnoreCase))
}

function Assert-RecordingsAreSafe {
    # Where the app saves recordings: save_dir in the user's config.json
    # (UTF-8), else the default folder in the user profile.
    $saveDir = Join-Path $env:USERPROFILE "Meeting Notes"
    $configFile = Join-Path (Join-Path $env:USERPROFILE ".meeting-notes") "config.json"
    if (Test-Path -LiteralPath $configFile) {
        try {
            $cfg = [IO.File]::ReadAllText($configFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
            $configured = [string]$cfg.save_dir
            if (-not [string]::IsNullOrWhiteSpace($configured)) { $saveDir = $configured.Trim() }
        } catch { }
    }
    try {
        if ($saveDir -eq "~" -or $saveDir.StartsWith("~\") -or $saveDir.StartsWith("~/")) {
            $saveDir = $env:USERPROFILE + $saveDir.Substring(1)
        }
        # The shortcut starts the app inside the install folder, so a relative
        # save folder resolves there.
        if (-not [IO.Path]::IsPathRooted($saveDir)) { $saveDir = Join-Path $installDir $saveDir }
        $saveDir = Get-NormalizedPath $saveDir
    } catch { }

    # The install folder and any leftover MeetingNotes.old-* folders are what
    # the scripts remove.
    $roots = @($installDir)
    $parent = Split-Path -Parent $installDir
    if (Test-Path -LiteralPath $parent) {
        $roots += @(Get-ChildItem -LiteralPath $parent -Directory -Filter "MeetingNotes.old-*" -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName })
    }

    foreach ($root in $roots) {
        $inside = $false
        try { $inside = Test-PathInside $saveDir $root } catch { }
        if ($inside) {
            throw "Your recordings folder is inside the app folder ($saveDir). Removing the app folder would delete your recordings. Open Meeting Notes and use Move recordings, or change the folder in Settings, then run the __ACTION__ again. Nothing was changed."
        }
    }

    # Belt and braces: never remove a folder that holds recordings or the
    # upload queue, wherever the config says recordings live.
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $wav = Get-ChildItem -LiteralPath $root -Recurse -Force -File -Filter "*.wav" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        $queue = Get-ChildItem -LiteralPath $root -Recurse -Force -Directory -Filter ".upload-queue" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        $found = if ($null -ne $wav) { $wav.FullName } elseif ($null -ne $queue) { $queue.FullName } else { $null }
        if ($found) {
            throw "Recordings were found inside the app folder ($found). Removing the app folder would delete them. Open Meeting Notes and use Move recordings, or change the folder in Settings, then run the __ACTION__ again. Nothing was changed."
        }
    }
    return $saveDir
}
'''


def render_client_installer(server_address: str) -> str:
    """A dependency-complete, configured Windows installer bootstrap."""
    address = json.dumps(server_address.rstrip("/"))
    manifest = json.dumps(server_address.rstrip("/") + "/install/client-manifest.json")
    # No [CmdletBinding()] / param() header: the script takes no parameters, and it is usually run as
    # "irm ... | iex", where an attribute header has failed to parse on some machines ("Unexpected attribute").
    script = r'''#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$serverAddress = __SERVER_ADDRESS__
$manifestUrl = __MANIFEST_URL__
$installDir = Join-Path $env:LOCALAPPDATA "MeetingNotes"
$configDir = Join-Path $env:USERPROFILE ".meeting-notes"
$configPath = Join-Path $configDir "config.json"
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ("MeetingNotes-" + [Guid]::NewGuid().ToString("N"))
$archive = Join-Path $tempDir "MeetingNotes-Windows.zip"
$expanded = Join-Path $tempDir "expanded"
$staging = "$installDir.new"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$logDir = Join-Path $configDir "logs"
$logPath = Join-Path $logDir "update.log"
$isUpdate = [bool]$env:MEETING_NOTES_UPDATE
$installOk = $false

# Every run appends timestamped lines to update.log (the in-app updater of older
# clients does not set MEETING_NOTES_UPDATE, so logging cannot depend on it).
# Logging must never break an install: every failure here is swallowed.
function Write-Log([string]$message) {
    try {
        $line = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " " + $message + [Environment]::NewLine
        [IO.File]::AppendAllText($logPath, $line, $utf8NoBom)
    } catch { }
}

function Initialize-Log {
    try {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        # Keep the log small: past 256 KB keep only the last 200 lines.
        if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 262144) {
            $tail = @(Get-Content -LiteralPath $logPath -Tail 200 -Encoding UTF8)
            [IO.File]::WriteAllLines($logPath, [string[]]$tail, $utf8NoBom)
        }
    } catch { }
    Write-Log ("---- installer started (pid $PID, update=" + [int]$isUpdate + ", server=$serverAddress)")
}
Initialize-Log

# Log any terminating error (including the early checks below) and let it
# propagate unchanged, so the exit code and console output stay as they were.
trap {
    Write-Log ("ERROR: " + $_.Exception.Message)
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber) {
        Write-Log ("  at line " + $_.InvocationInfo.ScriptLineNumber + ": " + $_.InvocationInfo.Line.Trim())
    }
    Write-Log "installer finished: FAILED"
    break
}

# This is intentionally a per-user install. Do not add elevation, drivers,
# services, HKLM writes, or Program Files paths: Windows WASAPI loopback works
# without them and meetings must remain recordable by a standard user account.

function Write-Step([string]$message) {
    Write-Log "==> $message"
    Write-Host "`n==> $message" -ForegroundColor Cyan
}

function Set-DefaultProperty($object, [string]$name, $value) {
    if ($null -eq $object.PSObject.Properties[$name]) {
        $object | Add-Member -MemberType NoteProperty -Name $name -Value $value
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This installer is for Windows only."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Meeting Notes requires 64-bit Windows 10 or 11."
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# The in-app updater starts this script with the app folder as its working
# directory, and Windows refuses to rename a folder any process (this one
# included) is "in". Step out of it before anything else.
$neutralDir = [IO.Path]::GetTempPath()
Set-Location -LiteralPath $neutralDir
[Environment]::CurrentDirectory = $neutralDir

__RECORDINGS_GUARD__
$recordingsDir = Assert-RecordingsAreSafe
$stoppedApp = $false

function Restart-PreviousApp {
    # The update failed after the running app was closed: bring it back so the
    # window never just disappears.
    $oldExe = Join-Path $installDir "MeetingNotes.exe"
    if ($stoppedApp -and (Test-Path -LiteralPath $oldExe)) {
        try { Start-Process -FilePath $oldExe -WorkingDirectory $installDir | Out-Null } catch { }
    }
}

try {
    Write-Step "Downloading the self-contained Meeting Notes client"
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null
    Write-Log "manifest: $manifestUrl"
    $manifest = Invoke-RestMethod -UseBasicParsing -Uri $manifestUrl
    if (-not $manifest.url -or -not $manifest.sha256 -or $manifest.size -lt 1) {
        throw "The server returned an invalid client manifest."
    }
    $downloadUrl = [Uri]$manifest.url
    $expectedSize = [Int64]$manifest.size
    $expectedHash = ([string]$manifest.sha256).ToLowerInvariant()
    if ($downloadUrl.Scheme -ne "http" -and $downloadUrl.Scheme -ne "https") {
        throw "The client package URL is invalid."
    }
    if ($downloadUrl.Host -ne ([Uri]$manifestUrl).Host -or $downloadUrl.Port -ne ([Uri]$manifestUrl).Port) {
        throw "The client package URL must be hosted by the same server."
    }
    Write-Log ("package: " + $downloadUrl.AbsoluteUri + " (expecting $expectedSize bytes, version " + $manifest.version + ")")
    Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl.AbsoluteUri -OutFile $archive
    $actualSize = (Get-Item -LiteralPath $archive).Length
    Write-Log "downloaded $actualSize bytes"
    if ($actualSize -ne $expectedSize) {
        throw "The downloaded package size does not match the server manifest."
    }
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "The downloaded package hash does not match the server manifest."
    }
    Write-Log "package size and SHA-256 verified"
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded -Force
    $sourceExe = Get-ChildItem -LiteralPath $expanded -Filter "MeetingNotes.exe" -File -Recurse |
        Select-Object -First 1
    if ($null -eq $sourceExe) {
        throw "The release package does not contain MeetingNotes.exe."
    }

    Write-Step "Installing the application and bundled dependencies"
    Get-Process -Name "MeetingNotes" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            if ($_.Path -and $_.Path.StartsWith($installDir, [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $_.Id -Force
                $_.WaitForExit(5000)
                $script:stoppedApp = $true
            }
        } catch { }
    }
    # The GPU helper's runtime lives outside the app folder (%LOCALAPPDATA%\MeetingNotes-GPU) and is never
    # deleted on update. Stop it while the app is replaced; the app starts it again when it launches.
    $gpuRuntimeDir = Join-Path $env:LOCALAPPDATA "MeetingNotes-GPU"
    Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            if ($_.Path -and $_.Path.StartsWith($gpuRuntimeDir + "\", [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $_.Id -Force
                $_.WaitForExit(5000)
            }
        } catch { }
    }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Copy-Item -Path (Join-Path $sourceExe.DirectoryName "*") -Destination $staging -Recurse -Force
    Get-ChildItem -LiteralPath $staging -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath (Join-Path $staging "MeetingNotes.exe"))) {
        throw "The staged application failed validation."
    }
    # Swap folders instead of deleting in place: if any file of the old
    # install is locked (antivirus, Explorer), the rename fails before
    # anything is removed and the old version keeps working.
    $previous = "$installDir.old-" + [Guid]::NewGuid().ToString("N")
    if (Test-Path -LiteralPath $installDir) {
        # A just-stopped process or an antivirus scan can hold a handle for a
        # moment; retry briefly before giving up.
        $renamed = $false
        for ($attempt = 1; $attempt -le 10 -and -not $renamed; $attempt++) {
            try {
                Rename-Item -LiteralPath $installDir -NewName (Split-Path -Leaf $previous)
                $renamed = $true
            } catch {
                Start-Sleep -Milliseconds 500
            }
        }
        if (-not $renamed) {
            Restart-PreviousApp
            throw "Meeting Notes could not be updated because a file in $installDir is in use. Close any program using it and run the installer again. The installed version was left unchanged."
        }
    }
    try {
        Move-Item -LiteralPath $staging -Destination $installDir
    } catch {
        if (Test-Path -LiteralPath $previous) { Rename-Item -LiteralPath $previous -NewName (Split-Path -Leaf $installDir) }
        Restart-PreviousApp
        throw
    }
    # Best effort: remove this and any earlier leftovers of previous versions.
    Get-ChildItem -LiteralPath (Split-Path -Parent $installDir) -Directory -Filter "MeetingNotes.old-*" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

    Write-Step "Writing client configuration without replacing existing secrets"
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    $config = $null
    if (Test-Path -LiteralPath $configPath) {
        try {
            # Windows PowerShell 5.1 reads BOM-less files as ANSI, which would
            # corrupt non-ASCII device names and folders on every update.
            $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
        } catch {
            $backup = "$configPath.invalid-$(Get-Date -Format yyyyMMdd-HHmmss)"
            Copy-Item -LiteralPath $configPath -Destination $backup
            Write-Log "previous config was invalid; backed up to $backup"
            Write-Warning "The previous config was invalid and was backed up to $backup"
        }
    }
    if ($null -eq $config) { $config = [PSCustomObject]@{} }
    if ($null -eq $config.PSObject.Properties["server"]) {
        $config | Add-Member -MemberType NoteProperty -Name "server" -Value ([PSCustomObject]@{})
    }
    Set-DefaultProperty $config.server "token" ""
    Set-DefaultProperty $config.server "live_preview" $true
    Set-DefaultProperty $config.server "auto_upload" $true
    if ($null -eq $config.server.PSObject.Properties["url"]) {
        $config.server | Add-Member -MemberType NoteProperty -Name "url" -Value $serverAddress
    } else {
        $config.server.url = $serverAddress
    }
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 8), $utf8NoBom)

    $exe = Join-Path $installDir "MeetingNotes.exe"
    $guidePath = Join-Path $installDir "How to run Meeting Notes.txt"
    $guide = @"
MEETING NOTES - HOW TO RUN

1. Open Meeting Notes from the Start Menu or desktop shortcut.
2. Select Settings.
3. Confirm the Server URL is: $serverAddress
4. Enter the same server token used to sign in to the Meeting Notes website.
5. Choose where recordings should be saved, then select Save.
6. Confirm the microphone and system-audio devices shown in the main window.
7. Enter a meeting name (optional) and select Start recording.
8. Select Stop recording when the meeting ends. Upload and transcription are automatic.

The client is self-contained. Python, Qt, NumPy, SoundCard, HTTP, WebSocket,
and native runtime dependencies are included. No driver or administrator access
is required on 64-bit Windows 10 or 11.

Configuration: $configPath
Application:   $installDir
Server:        $serverAddress

If Remote Desktop does not expose a microphone, verify once from the physical
Windows session. If the server rejects the token, update it under Settings.
"@
    [IO.File]::WriteAllText($guidePath, $guide, $utf8NoBom)

    Write-Step "Creating Start Menu and desktop shortcuts"
    # Resolve the shell folders for the current user.  In particular, do not
    # use a hard-coded Desktop path: OneDrive-backed profiles return their
    # redirected Desktop folder from GetFolderPath.  Creating the folders is
    # harmless and keeps a partially fresh profile from losing the shortcut.
    $desktopDir = [Environment]::GetFolderPath("Desktop")
    $programsDir = [Environment]::GetFolderPath("Programs")
    New-Item -ItemType Directory -Path $desktopDir -Force | Out-Null
    New-Item -ItemType Directory -Path $programsDir -Force | Out-Null
    $shortcutPaths = @(
        (Join-Path $desktopDir "Meeting Notes.lnk"),
        (Join-Path $programsDir "Meeting Notes.lnk")
    )
    $shell = New-Object -ComObject WScript.Shell
    foreach ($shortcutPath in $shortcutPaths) {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $exe
        $shortcut.WorkingDirectory = $installDir
        $shortcut.Description = "Record and transcribe meetings"
        # MeetingNotes.exe carries the embedded release icon, so both the
        # desktop shortcut and the taskbar process show the same branding.
        $shortcut.IconLocation = "$exe,0"
        $shortcut.Save()
    }

    Write-Step "Checking the transcription server"
    try {
        $health = Invoke-RestMethod -UseBasicParsing -Uri ($serverAddress + "/health") -TimeoutSec 10
        if ($health.status -eq "ok") {
            Write-Host "Server is reachable: $serverAddress" -ForegroundColor Green
        } else {
            Write-Log "WARNING: the server responded but did not report healthy status"
            Write-Warning "The server responded but did not report healthy status."
        }
    } catch {
        Write-Log "WARNING: the client was installed, but the server is not reachable yet: $serverAddress"
        Write-Warning "The client was installed, but the server is not reachable yet: $serverAddress"
    }

    Write-Step "Installation complete"
    $installOk = $true
    Write-Host "Launch from the Start Menu or desktop shortcut."
    Write-Host "On first run, open Settings and enter the server login token."
    Write-Host "A copy of the guide is at: $guidePath"
    $process = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds 2
    if ($process.HasExited) {
        Write-Log "WARNING: Meeting Notes exited during startup"
        Write-Warning "Meeting Notes exited during startup. Re-run the installer or report the startup failure."
    }
} finally {
    if ($installOk) { Write-Log "installer finished: OK" }
    if (Test-Path -LiteralPath $tempDir) { Remove-Item -LiteralPath $tempDir -Recurse -Force }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
'''
    script = script.replace("__RECORDINGS_GUARD__", _RECORDINGS_GUARD_PS.replace("__ACTION__", "installer"))
    return script.replace("__SERVER_ADDRESS__", address).replace("__MANIFEST_URL__", manifest)


def render_mac_installer(server_address: str) -> str:
    """The configured macOS installer script (``curl ... /install/mac.sh | bash``)."""
    from meeting_notes.server import mac_installer

    return mac_installer.render_mac_installer(server_address)


def render_mac_uninstaller() -> str:
    """The macOS removal script (``curl ... /install/uninstall-mac.sh | bash``)."""
    from meeting_notes.server import mac_installer

    return mac_installer.render_mac_uninstaller()


def render_client_uninstaller() -> str:
    """Generate a non-elevated, per-user Windows client removal script."""
    script = r'''#Requires -Version 5.1
[CmdletBinding()]
param([switch]$RemoveSettings)

$ErrorActionPreference = "Stop"
$installDir = Join-Path $env:LOCALAPPDATA "MeetingNotes"
$settingsDir = Join-Path $env:USERPROFILE ".meeting-notes"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Meeting Notes.lnk"
$startMenuShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Meeting Notes.lnk"

# Windows cannot delete a folder a process is "in"; step out of the app folder.
$neutralDir = [IO.Path]::GetTempPath()
Set-Location -LiteralPath $neutralDir
[Environment]::CurrentDirectory = $neutralDir

__RECORDINGS_GUARD__
$recordingsDir = Assert-RecordingsAreSafe

Write-Host "Stopping Meeting Notes processes installed under $installDir..."
Get-Process -Name "MeetingNotes" -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ($_.Path -and $_.Path.StartsWith($installDir, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id $_.Id -Force
            $_.WaitForExit(5000)
        }
    } catch { }
}

# GPU helper: stop its runtime, remove the start-at-login entry and the downloaded runtime
# (%LOCALAPPDATA%\MeetingNotes-GPU). This needs no administrator rights; anything already missing is ignored.
$gpuRuntimeDir = Join-Path $env:LOCALAPPDATA "MeetingNotes-GPU"
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ($_.Path -and $_.Path.StartsWith($gpuRuntimeDir + "\", [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id $_.Id -Force
            $_.WaitForExit(5000)
        }
    } catch { }
}
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "MeetingNotes" -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $gpuRuntimeDir) {
    Remove-Item -LiteralPath $gpuRuntimeDir -Recurse -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $installDir) {
    Remove-Item -LiteralPath $installDir -Recurse -Force
}
Get-ChildItem -LiteralPath (Split-Path -Parent $installDir) -Directory -Filter "MeetingNotes.old-*" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
foreach ($shortcut in @($desktopShortcut, $startMenuShortcut)) {
    if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut -Force }
}
if ($RemoveSettings -and (Test-Path -LiteralPath $settingsDir) -and (Test-PathInside $recordingsDir $settingsDir)) {
    # The recordings folder lives inside the settings folder: never delete it.
    Write-Warning "Client settings were kept because your recordings folder is inside $settingsDir."
} elseif ($RemoveSettings -and (Test-Path -LiteralPath $settingsDir)) {
    Remove-Item -LiteralPath $settingsDir -Recurse -Force
    Write-Host "Removed client settings."
} else {
    Write-Host "Recordings and client settings were preserved."
}
Write-Host "Meeting Notes was uninstalled for this Windows user."
'''
    return script.replace("__RECORDINGS_GUARD__", _RECORDINGS_GUARD_PS.replace("__ACTION__", "uninstaller"))
