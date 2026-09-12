<#
.SYNOPSIS
    Sets up and flashes the N3005Q flight board to an M5Stack M5Paper Color.

.DESCRIPTION
    Runs the whole chain: prerequisite checks, config.h creation, git repo and
    GitHub push, PlatformIO install, build, flash, serial monitor.

    Safe to re-run. Every step checks whether it is already done first.

    Your wifi password is typed by you into a masked prompt and written only to
    firmware/n3005q_board/config.h, which is gitignored. The script refuses to
    write it if the gitignore entry is missing.

.EXAMPLE
    .\setup.ps1
    Full run, prompting for anything it needs.

.EXAMPLE
    .\setup.ps1 -MonitorOnly
    Just reattach the serial monitor to an already-flashed board.

.EXAMPLE
    .\setup.ps1 -SkipGit
    Config and flash only, leaving the repo alone.
#>
[CmdletBinding()]
param(
    [string] $CardUrl,
    [switch] $SkipGit,
    [switch] $SkipFlash,
    [switch] $MonitorOnly
)

$ErrorActionPreference = 'Stop'
# PowerShell 7.3+ can turn a nonzero exit from a native command into a
# terminating error. This script checks exit codes itself, so switch it off.
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$script:Root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$script:Firmware = Join-Path $Root 'firmware'
$script:ConfigH = Join-Path $Firmware 'n3005q_board\config.h'
$script:Warnings = @()

# ------------------------------------------------------------------ output
function Write-Step { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    OK   $m" -ForegroundColor Green }
function Write-Info { param($m) Write-Host "         $m" -ForegroundColor DarkGray }
function Write-Warn2 {
    param($m)
    Write-Host "    WARN $m" -ForegroundColor Yellow
    $script:Warnings += $m
}
function Die { param($m) Write-Host "`n    FAIL $m" -ForegroundColor Red; exit 1 }

function Test-Cmd {
    param($Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

# Windows has three plausible python spellings. Find one that actually runs.
function Find-Python {
    foreach ($c in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = $c[0]
        if (-not (Test-Cmd $exe)) { continue }
        try {
            $probe = (Get-PyPre $c) + @('--version')
            & $exe @probe 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { return , $c }
        } catch { }
    }
    return $null
}

# PowerShell's range operator makes 1..0 into @(1,0), so naively slicing a
# one-element command array yields the command back and you end up running
# 'python python --version'. Always go through this.
function Get-PyPre {
    param([string[]] $PyCmd)
    if ($PyCmd.Count -gt 1) { return , @($PyCmd[1..($PyCmd.Count - 1)]) }
    return , @()
}

# Runs python, streaming output to the console. Caller checks $LASTEXITCODE.
function Invoke-Py {
    param([string[]] $PyCmd, [string[]] $Arguments, [switch] $Quiet)
    $all = (Get-PyPre $PyCmd) + $Arguments
    if ($Quiet) { & $PyCmd[0] @all 2>&1 | Out-Null }
    else        { & $PyCmd[0] @all }
}

# Runs python and captures stdout as a single string.
function Get-PyOutput {
    param([string[]] $PyCmd, [string[]] $Arguments)
    $all = (Get-PyPre $PyCmd) + $Arguments
    return ((& $PyCmd[0] @all 2>&1) -join ' ').Trim()
}

# ------------------------------------------------------------------ banner
Write-Host ""
Write-Host "  N3005Q flight board setup" -ForegroundColor White
Write-Host "  M5Stack M5Paper Color / ESP32-S3" -ForegroundColor DarkGray
Write-Host "  $Root" -ForegroundColor DarkGray

if (-not (Test-Path (Join-Path $Firmware 'platformio.ini'))) {
    Die "firmware\platformio.ini not found. Run this from inside n3005q-board."
}

# ------------------------------------------------------------- 1 preflight
Write-Step "Checking prerequisites"

$py = Find-Python
if (-not $py) {
    Die "No working Python found. Install from python.org or 'winget install Python.Python.3.12', then re-run."
}
$pyVer = Get-PyOutput $py @('--version')
Write-Ok "Python: $pyVer  (via '$($py -join ' ')')"

if (Test-Cmd 'git') {
    Write-Ok "git: $((git --version) -replace 'git version ','')"
} else {
    if (-not $SkipGit) {
        Write-Warn2 "git not found. Skipping repo steps; flashing will still work."
        $SkipGit = $true
    }
}

$hasGh = Test-Cmd 'gh'
if ($hasGh) { Write-Ok "GitHub CLI present" } else { Write-Info "GitHub CLI not found (repo creation will be manual)" }

# --------------------------------------------------- 2 workflow file exists
Write-Step "Checking GitHub Actions workflow"
$wfDir = Join-Path $Root '.github\workflows'
$wf = Join-Path $wfDir 'render.yml'
if (Test-Path $wf) {
    Write-Ok "render.yml present"
} else {
    New-Item -ItemType Directory -Force -Path $wfDir | Out-Null
    # Single-quoted here-string: no PowerShell interpolation, so GitHub's
    # ${{ }} expressions survive intact.
    $wfBody = @'
name: render card

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
  push:
    branches: [main]
    paths: ["render/**", ".github/workflows/render.yml"]

concurrency:
  group: render
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  render:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: render/requirements.txt

      - run: pip install -r render/requirements.txt

      - name: Render card
        env:
          N3005Q_OUT: public
          FAA_CLIENT_ID: ${{ secrets.FAA_CLIENT_ID }}
          FAA_CLIENT_SECRET: ${{ secrets.FAA_CLIENT_SECRET }}
        run: python render/render.py

      - name: Verify before publishing
        working-directory: render
        env:
          N3005Q_CARD: ../public/card.png
        run: python verify.py

      - name: Skip if unchanged
        id: changed
        run: |
          git fetch --depth=1 origin output || true
          if git cat-file -e origin/output:public/card.png 2>/dev/null; then
            git show origin/output:public/card.png > /tmp/prev.png
            if cmp -s /tmp/prev.png public/card.png; then
              echo "identical render, nothing to publish"
              echo "skip=true" >> "$GITHUB_OUTPUT"
            fi
          fi

      - name: Publish to output branch
        if: steps.changed.outputs.skip != 'true'
        run: |
          git config user.name  "n3005q-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git checkout --orphan publish
          git reset
          git add -f public vercel.json
          git commit -q -m "render $(date -u +%Y-%m-%dT%H:%M:%SZ)"
          git push -f origin publish:output
'@
    Set-Content -Path $wf -Value $wfBody -Encoding UTF8
    Write-Ok "Wrote .github\workflows\render.yml"
}

if ($MonitorOnly) {
    Write-Step "Serial monitor (Ctrl+C to exit)"
    Invoke-Py $py @('-m', 'platformio', 'device', 'monitor', '-d', $Firmware)
    exit 0
}

# -------------------------------------------------------------- 3 config.h
Write-Step "Firmware config"

if (Test-Path $ConfigH) {
    Write-Ok "config.h already exists (delete it to re-enter settings)"
    $existing = Get-Content $ConfigH -Raw
    if ($existing -match 'CARD_URL\s+"([^"]+)"') { $CardUrl = $Matches[1] }
} else {
    # Refuse to write a plaintext password into a file git might track.
    $gi = Join-Path $Root '.gitignore'
    $ignored = (Test-Path $gi) -and ((Get-Content $gi -Raw) -match 'config\.h')
    if (-not $ignored) {
        Die "config.h is not in .gitignore. Refusing to write your wifi password into a tracked file."
    }
    Write-Ok ".gitignore covers config.h"

    Write-Host ""
    Write-Host "    This board has a 2.4 GHz radio only. A 5 GHz-only SSID will never connect." -ForegroundColor Yellow
    $ssid = Read-Host "    Wifi SSID"
    if (-not $ssid) { Die "SSID cannot be empty." }

    $secure = Read-Host "    Wifi password" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $pass = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }

    if (-not $CardUrl) {
        Write-Host ""
        Write-Host "    Where the rendered card is served. Must be HTTPS." -ForegroundColor DarkGray
        Write-Host "    Vercel:  https://<project>.vercel.app/card.png" -ForegroundColor DarkGray
        Write-Host "    or raw:  https://raw.githubusercontent.com/<user>/<repo>/output/public/card.png" -ForegroundColor DarkGray
        $CardUrl = Read-Host "    CARD_URL"
    }
    if ($CardUrl -notmatch '^https://') { Die "CARD_URL must start with https://" }

    # Escape for a C string literal.
    function Esc { param($s) $s -replace '\\', '\\' -replace '"', '\"' }

    $tpl = Get-Content (Join-Path $Firmware 'n3005q_board\config.h.example') -Raw
    $tpl = $tpl -replace '(?m)^#define WIFI_SSID.*$',     ('#define WIFI_SSID       "' + (Esc $ssid)   + '"')
    $tpl = $tpl -replace '(?m)^#define WIFI_PASSWORD.*$', ('#define WIFI_PASSWORD   "' + (Esc $pass)   + '"')
    $tpl = $tpl -replace '(?m)^#define CARD_URL.*$',      ('#define CARD_URL        "' + (Esc $CardUrl)+ '"')
    Set-Content -Path $ConfigH -Value $tpl -Encoding ASCII
    $pass = $null
    Write-Ok "Wrote config.h (gitignored, contains your password in plaintext)"
}

# ------------------------------------------------------- 4 is the URL live?
if ($CardUrl) {
    Write-Step "Checking the card URL"
    try {
        $r = Invoke-WebRequest -Uri $CardUrl -Method Head -TimeoutSec 20 -UseBasicParsing
        $ct = $r.Headers['Content-Type']
        $len = $r.Headers['Content-Length']
        if ($ct -like 'image/png*') {
            Write-Ok "$CardUrl -> HTTP $($r.StatusCode), $ct, $len bytes"
        } else {
            Write-Warn2 "URL responded but content-type is '$ct', expected image/png."
        }
    } catch {
        Write-Warn2 "Could not fetch $CardUrl -- $($_.Exception.Message)"
        Write-Info "Flashing will still work, but the panel will not paint until this URL serves a PNG."
    }
}

# ------------------------------------------------------------------ 5 repo
if (-not $SkipGit) {
    Write-Step "Git repository"
    Push-Location $Root
    try {
        if (-not (Test-Path (Join-Path $Root '.git'))) {
            git init -q
            git symbolic-ref HEAD refs/heads/main
            Write-Ok "Initialised repo on main"
        } else {
            Write-Ok "Repo already initialised"
        }

        git add -A 2>&1 | Out-Null
        $staged = git diff --cached --name-only
        if ($staged) {
            git -c user.name='n3005q' -c user.email='n3005q@localhost' commit -q -m "N3005Q flight board" 2>&1 | Out-Null
            Write-Ok "Committed $((($staged -split "`n") | Measure-Object).Count) file(s)"
        } else {
            Write-Ok "Nothing new to commit"
        }

        # Guard against the one mistake that actually matters here.
        $tracked = git ls-files --error-unmatch 'firmware/n3005q_board/config.h' 2>$null
        if ($tracked) { Die "config.h is tracked by git. Run: git rm --cached firmware/n3005q_board/config.h" }

        $origin = git remote get-url origin 2>$null
        if ($origin) {
            Write-Ok "Remote: $origin"
            Write-Info "Pushing..."
            git push -u origin main
            if ($LASTEXITCODE -ne 0) { Write-Warn2 "git push failed. Push manually, then re-run." }
        } elseif ($hasGh) {
            gh auth status 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Warn2 "GitHub CLI is not logged in. Run 'gh auth login', then re-run this script."
            } else {
                Write-Host ""
                Write-Host "    Public repos can be read by raw.githubusercontent.com, which lets you" -ForegroundColor DarkGray
                Write-Host "    skip Vercel for a first test. Private repos cannot." -ForegroundColor DarkGray
                $vis = Read-Host "    Create GitHub repo as [pub]lic or [priv]ate? (or 'skip')"
                switch -Regex ($vis) {
                    '^pub'  { gh repo create $(Split-Path $Root -Leaf) --source=. --public  --push; break }
                    '^priv' { gh repo create $(Split-Path $Root -Leaf) --source=. --private --push; break }
                    default { Write-Info "Skipped repo creation." }
                }
            }
        } else {
            Write-Info "No remote set. Create a repo on GitHub, then:"
            Write-Info "  git remote add origin <url> ; git push -u origin main"
        }
    } finally {
        Pop-Location
    }
}

# ----------------------------------------------------------- 6 platformio
Write-Step "PlatformIO"
Invoke-Py $py @('-m', 'platformio', '--version') -Quiet
if ($LASTEXITCODE -ne 0) {
    Write-Info "Installing (this takes a minute)..."
    Invoke-Py $py @('-m', 'pip', 'install', '--user', '--upgrade', 'platformio') -Quiet
    if ($LASTEXITCODE -ne 0) { Die "PlatformIO install failed. Try: $($py -join ' ') -m pip install platformio" }
}
Write-Ok (Get-PyOutput $py @('-m', 'platformio', '--version'))

if ($SkipFlash) {
    Write-Step "Done (flash skipped)"
    exit 0
}

# ---------------------------------------------------------- 7 build + flash
Write-Step "Serial ports"
Invoke-Py $py @('-m', 'platformio', 'device', 'list')

Write-Step "Building"
Write-Info "First build downloads the ESP32 toolchain and M5Stack libraries. Several minutes."
Invoke-Py $py @('-m', 'platformio', 'run', '-d', $Firmware)
if ($LASTEXITCODE -ne 0) { Die "Build failed. Send me the output above; the firmware is mine to fix." }
Write-Ok "Built"

Write-Step "Flashing"
Write-Info "If the port is not found, hold BOOT, tap RST, release BOOT, then re-run."
Invoke-Py $py @('-m', 'platformio', 'run', '-t', 'upload', '-d', $Firmware)
if ($LASTEXITCODE -ne 0) { Die "Upload failed. Check the USB-C cable is a data cable, not charge-only." }
Write-Ok "Flashed"

# ---------------------------------------------------------------- 8 monitor
Write-Host ""
if ($script:Warnings.Count) {
    Write-Host "  $($script:Warnings.Count) warning(s):" -ForegroundColor Yellow
    foreach ($w in $script:Warnings) { Write-Host "    - $w" -ForegroundColor Yellow }
}
Write-Host ""
Write-Host "  Expect on the monitor:" -ForegroundColor White
Write-Host "    wifi ok, rssi -NN"        -ForegroundColor DarkGray
Write-Host "    HTTP 200"                 -ForegroundColor DarkGray
Write-Host "    read ~11700 bytes"        -ForegroundColor DarkGray
Write-Host "    panel refresh took 15000-30000 ms   <- the real confirmation" -ForegroundColor DarkGray
Write-Host "    sleeping NN min"          -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Ctrl+C to exit the monitor. The board keeps running." -ForegroundColor DarkGray
Write-Step "Serial monitor"
Invoke-Py $py @('-m', 'platformio', 'device', 'monitor', '-d', $Firmware)
