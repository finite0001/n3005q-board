#!/usr/bin/env bash
# Sets up and flashes the N3005Q flight board to an M5Stack M5Paper Color.
# macOS and Linux. For Windows use setup.ps1.
#
# Safe to re-run: every step checks whether it is already done.
#
# Your wifi password is typed into a hidden prompt and written only to
# firmware/n3005q_board/config.h, which is gitignored. The script refuses to
# write it if that gitignore entry is missing.
#
#   bash setup.sh                 full run
#   bash setup.sh --monitor-only  reattach the serial monitor
#   bash setup.sh --skip-git      config and flash only
#   bash setup.sh --skip-flash    everything except build and flash

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE="$ROOT/firmware"
CONFIG_H="$FIRMWARE/n3005q_board/config.h"
VENV="$ROOT/.venv"
CARD_URL=""
SKIP_GIT=0; SKIP_FLASH=0; MONITOR_ONLY=0
WARNINGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --card-url)     CARD_URL="${2:-}"; shift 2 ;;
    --skip-git)     SKIP_GIT=1; shift ;;
    --skip-flash)   SKIP_FLASH=1; shift ;;
    --monitor-only) MONITOR_ONLY=1; shift ;;
    -h|--help)      sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  C=$'\033[36m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[90m'; Z=$'\033[0m'
else
  C=""; G=""; Y=""; R=""; D=""; Z=""
fi
step() { printf '\n%s==> %s%s\n' "$C" "$1" "$Z"; }
ok()   { printf '    %sOK%s   %s\n' "$G" "$Z" "$1"; }
info() { printf '         %s%s%s\n' "$D" "$1" "$Z"; }
warn() { printf '    %sWARN%s %s\n' "$Y" "$Z" "$1"; WARNINGS+=("$1"); }
die()  { printf '\n    %sFAIL%s %s\n' "$R" "$Z" "$1"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

printf '\n  N3005Q flight board setup\n'
printf '  %sM5Stack M5Paper Color / ESP32-S3%s\n' "$D" "$Z"
printf '  %s%s%s\n' "$D" "$ROOT" "$Z"

[ -f "$FIRMWARE/platformio.ini" ] || die "firmware/platformio.ini not found. Run this from inside n3005q-board."

# ----------------------------------------------------------------- preflight
step "Checking prerequisites"

PY=""
for c in python3 python; do
  if have "$c" && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || die "No Python 3.8+ found. On macOS: 'xcode-select --install' or 'brew install python'."
ok "Python: $("$PY" --version 2>&1)  ($(command -v "$PY"))"

if have git; then
  ok "git: $(git --version | sed 's/git version //')"
elif [ "$SKIP_GIT" -eq 0 ]; then
  warn "git not found; skipping repo steps. Flashing still works."
  SKIP_GIT=1
fi

if have gh; then ok "GitHub CLI present"; else info "GitHub CLI not found (repo creation will be manual)"; fi

case "$(uname -s)" in
  Darwin) ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '')"
          PORT_GLOB="/dev/cu.usbmodem*" ;;
  *)      ok "$(uname -s)"; PORT_GLOB="/dev/ttyACM* /dev/ttyUSB*" ;;
esac

# ------------------------------------------------------------ workflow file
step "GitHub Actions workflow"
WF="$ROOT/.github/workflows/render.yml"
if [ -f "$WF" ]; then
  ok "render.yml present"
else
  mkdir -p "$(dirname "$WF")"
  # Quoted heredoc: GitHub's ${{ }} and $GITHUB_OUTPUT must survive verbatim.
  cat > "$WF" <<'YAML'
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
YAML
  ok "Wrote .github/workflows/render.yml"
fi

# --------------------------------------------------------------- platformio
setup_pio() {
  if [ ! -x "$VENV/bin/pio" ]; then
    info "Creating venv and installing PlatformIO (a minute or two)..."
    "$PY" -m venv "$VENV" || die "venv creation failed."
    "$VENV/bin/pip" install --quiet --upgrade pip || true
    "$VENV/bin/pip" install --quiet platformio || die "PlatformIO install failed."
  fi
  PIO="$VENV/bin/pio"
  ok "$("$PIO" --version 2>&1)"
}

if [ "$MONITOR_ONLY" -eq 1 ]; then
  step "PlatformIO"; setup_pio
  step "Serial monitor (Ctrl+C to exit)"
  exec "$PIO" device monitor -d "$FIRMWARE"
fi

# ----------------------------------------------------------------- config.h
step "Firmware config"
if [ -f "$CONFIG_H" ]; then
  ok "config.h exists (delete it to re-enter settings)"
  CARD_URL="$(sed -n 's/^#define CARD_URL[[:space:]]*"\(.*\)"/\1/p' "$CONFIG_H" | head -1)"
else
  grep -q 'config\.h' "$ROOT/.gitignore" 2>/dev/null \
    || die "config.h is not in .gitignore. Refusing to write your wifi password into a trackable file."
  ok ".gitignore covers config.h"

  printf '\n    %sThis board has a 2.4 GHz radio only. A 5 GHz-only SSID will never connect.%s\n' "$Y" "$Z"
  printf '    Wifi SSID: '; IFS= read -r SSID
  [ -n "$SSID" ] || die "SSID cannot be empty."
  printf '    Wifi password (hidden): '; IFS= read -rs PASS; printf '\n'

  if [ -z "$CARD_URL" ]; then
    printf '\n    %sWhere the rendered card is served. Must be HTTPS.%s\n' "$D" "$Z"
    printf '    %sVercel: https://<project>.vercel.app/card.png%s\n' "$D" "$Z"
    printf '    %sor raw: https://raw.githubusercontent.com/<user>/<repo>/output/public/card.png%s\n' "$D" "$Z"
    printf '    CARD_URL: '; IFS= read -r CARD_URL
  fi
  case "$CARD_URL" in https://*) ;; *) die "CARD_URL must start with https://" ;; esac

  # Templating in python, not sed: it escapes C string literals correctly and
  # does not care what punctuation is in your password.
  SSID="$SSID" PASS="$PASS" CARD_URL="$CARD_URL" "$PY" - "$FIRMWARE/n3005q_board/config.h.example" "$CONFIG_H" <<'PYEOF'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
def esc(v): return v.replace('\\', '\\\\').replace('"', '\\"')
t = open(src).read()
for key, pad, val in (("WIFI_SSID", 7, os.environ["SSID"]),
                      ("WIFI_PASSWORD", 3, os.environ["PASS"]),
                      ("CARD_URL", 8, os.environ["CARD_URL"])):
    t = re.sub(rf'^#define {key}.*$',
               f'#define {key}{" " * pad}      "{esc(val)}"', t, flags=re.M)
open(dst, "w").write(t)
PYEOF
  unset PASS
  chmod 600 "$CONFIG_H"
  ok "Wrote config.h (gitignored, mode 600, holds your password in plaintext)"
fi

# -------------------------------------------------------------- is URL live?
if [ -n "$CARD_URL" ]; then
  step "Checking the card URL"
  HDRS="$(curl -sSL -I --max-time 20 "$CARD_URL" 2>&1)" && {
    CODE="$(printf '%s' "$HDRS" | awk 'tolower($1) ~ /^http/ {c=$2} END{print c}')"
    CT="$(printf '%s' "$HDRS" | awk 'tolower($1)=="content-type:" {print $2}' | tr -d '\r' | tail -1)"
    LEN="$(printf '%s' "$HDRS" | awk 'tolower($1)=="content-length:" {print $2}' | tr -d '\r' | tail -1)"
    case "$CT" in
      image/png*) ok "HTTP $CODE, $CT, ${LEN:-?} bytes" ;;
      *)          warn "URL responded HTTP $CODE with content-type '${CT:-none}', expected image/png." ;;
    esac
  } || warn "Could not reach $CARD_URL. Flashing works; the panel just will not paint yet."
fi

# --------------------------------------------------------------------- repo
if [ "$SKIP_GIT" -eq 0 ]; then
  step "Git repository"
  cd "$ROOT" || die "cannot cd to $ROOT"
  if [ ! -d .git ]; then
    git init -q && git symbolic-ref HEAD refs/heads/main
    ok "Initialised repo on main"
  else
    ok "Repo already initialised"
  fi
  git add -A >/dev/null 2>&1
  if git diff --cached --quiet 2>/dev/null; then
    ok "Nothing new to commit"
  else
    git -c user.name=n3005q -c user.email=n3005q@localhost commit -q -m "N3005Q flight board"
    ok "Committed"
  fi
  if git ls-files --error-unmatch firmware/n3005q_board/config.h >/dev/null 2>&1; then
    die "config.h is tracked by git. Run: git rm --cached firmware/n3005q_board/config.h"
  fi
  ok "config.h is not tracked"

  if ORIGIN="$(git remote get-url origin 2>/dev/null)"; then
    info "Remote: $ORIGIN"
    git push -u origin main || warn "git push failed. Push manually, then re-run."
  elif have gh && gh auth status >/dev/null 2>&1; then
    printf '\n    %sA public repo can be read via raw.githubusercontent.com, which lets you%s\n' "$D" "$Z"
    printf '    %sskip Vercel for a first test. A private repo cannot.%s\n' "$D" "$Z"
    printf '    Create GitHub repo as [pub]lic, [priv]ate, or [s]kip? '; IFS= read -r VIS
    case "$VIS" in
      pub*)  gh repo create "$(basename "$ROOT")" --source=. --public  --push ;;
      priv*) gh repo create "$(basename "$ROOT")" --source=. --private --push ;;
      *)     info "Skipped repo creation." ;;
    esac
  else
    info "No remote set. Create a repo on GitHub, then:"
    info "  git remote add origin <url> && git push -u origin main"
  fi
fi

# ---------------------------------------------------------------- build/flash
step "PlatformIO"; setup_pio
[ "$SKIP_FLASH" -eq 1 ] && { step "Done (flash skipped)"; exit 0; }

step "Serial ports"
# shellcheck disable=SC2086
FOUND="$(ls -1 $PORT_GLOB 2>/dev/null || true)"
if [ -n "$FOUND" ]; then
  printf '%s\n' "$FOUND" | while read -r p; do ok "$p"; done
else
  warn "No serial device matching $PORT_GLOB. Is the board plugged in with a DATA cable?"
  info "The ESP32-S3 uses native USB, so no driver is needed. Charge-only cables are the usual culprit."
fi

step "Building"
info "First build downloads the ESP32 toolchain and M5Stack libraries. Several minutes."
"$PIO" run -d "$FIRMWARE" || die "Build failed. Send me the output above; the firmware is mine to fix."
ok "Built"

step "Flashing"
info "If the port is not found, hold BOOT, tap RST, release BOOT, then re-run."
"$PIO" run -t upload -d "$FIRMWARE" || die "Upload failed. Check the cable, or try the BOOT/RST sequence above."
ok "Flashed"

if [ "${#WARNINGS[@]}" -gt 0 ]; then
  printf '\n  %s%d warning(s):%s\n' "$Y" "${#WARNINGS[@]}" "$Z"
  for w in "${WARNINGS[@]}"; do printf '    %s- %s%s\n' "$Y" "$w" "$Z"; done
fi

cat <<EOF

  Expect on the monitor:
    wifi ok, rssi -NN
    HTTP 200
    read ~11700 bytes
    panel refresh took 15000-30000 ms   <- the real confirmation
    sleeping NN min

  Ctrl+C exits the monitor. The board keeps running.
EOF

step "Serial monitor"
exec "$PIO" device monitor -d "$FIRMWARE"
