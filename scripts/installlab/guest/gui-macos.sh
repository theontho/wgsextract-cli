#!/bin/sh
# shellcheck disable=SC1007
# Install-lab guest scenario for the WGS Extract macOS app (Tauri).
#
# baseline snapshot -> download the DMG like a browser -> Gatekeeper ->
# drag-install to /Applications -> the app runs its own setup -> real actions
# through the app's bundled runtime -> snapshot -> the app's own uninstall ->
# drag the app to the Trash and empty it -> snapshot
#
# Inputs (environment), besides LAB_RESULTS / LAB_NONCE / LAB_MODE / LAB_SABOTAGE
# (see scenario.sh):
#   LAB_DMG_URL          the DMG under test
#   LAB_ARCHIVE_URL      optional CLI source archive for setup to install
#                        (unset: setup installs the CLI release the app pins)
#
# Gatekeeper is assessed either way; the host knows whether the DMG is a
# signed release (must pass) or a local ad-hoc build (must be rejected).
# Nothing here decides pass/fail; the host's oracle does, from the evidence.
set -u

R="${LAB_RESULTS:?}"
HERE="$(CDPATH= cd "$(dirname "$0")" && pwd)"
EVIDENCE="$R/evidence"
mkdir -p "$EVIDENCE" "$R/workload"
printf '%s\n' "${LAB_NONCE:?}" > "$R/nonce"
SABOTAGE="${LAB_SABOTAGE:-}"
MODE="${LAB_MODE:-install}"
DMG="$HOME/Downloads/WGSExtract.dmg"
APP="/Applications/WGSExtract.app"
BUNDLE_ID="dev.guiforcli.web.embed.wgsextract"
RESOURCES="$APP/Contents/Resources"
WORKSPACE="$HOME/Library/Application Support/$BUNDLE_ID/BundleWorkspaces/wgs-extract"
# macOS's Local Network privacy keeps an app from reaching the lab host, so the
# CLI tree under test reaches setup as a local file (users' setup downloads
# the pinned release from GitHub instead).
CLI_ARCHIVE="/tmp/wgse-lab-cli.tar.gz"

step() {
    name="$1"
    shift
    start=$(date +%s)
    "$@" > "$EVIDENCE/$name.log" 2>&1
    rc=$?
    echo "$rc" > "$EVIDENCE/$name.rc"
    echo $(( $(date +%s) - start )) > "$EVIDENCE/$name.seconds"
    echo "scenario step $name rc=$rc"
}

snapshot() {
    sh "$HERE/inventory.sh" "$R/inventory-$1.txt" "$R"
}

wait_for_file() {
    deadline=$(( $(date +%s) + $2 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        [ -s "$1" ] && return 0
        sleep 5
    done
    echo "timed out after $2 s waiting for $1"
    return 1
}

app_running() {
    pgrep -f "$APP/Contents/MacOS/" >/dev/null 2>&1
}

wait_for_app_exit() {
    deadline=$(( $(date +%s) + $1 ))
    while app_running; do
        [ "$(date +%s)" -lt "$deadline" ] || { echo "app still running"; return 1; }
        sleep 2
    done
}

download_dmg() {
    mkdir -p "$HOME/Downloads" &&
        curl -fsSL -o "$DMG" "$LAB_DMG_URL" &&
        # What a browser does to a download; Gatekeeper keys off it.
        xattr -w com.apple.quarantine "0083;$(printf '%x' "$(date +%s)");Safari;$(uuidgen)" "$DMG"
}

install_app() {
    mount="$(mktemp -d /tmp/wgse-dmg.XXXXXX)"
    hdiutil attach -nobrowse -readonly -mountpoint "$mount" "$DMG" || return 1
    # Drag to /Applications.
    ditto "$mount/WGSExtract.app" "$APP"
    rc=$?
    hdiutil detach "$mount" || rc=1
    rmdir "$mount"
    codesign --verify --deep --strict --verbose=2 "$APP" || rc=1
    return $rc
}

gatekeeper() {
    spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
    dmg_rc=$?
    spctl --assess --type execute --verbose=2 "$APP"
    app_rc=$?
    [ "$dmg_rc" = 0 ] && [ "$app_rc" = 0 ]
}

approve_first_launch() {
    # Clicking "Open" in the first-launch dialog; the lab can't click it.
    xattr -dr com.apple.quarantine "$APP"
}

launch_app() {
    # Launch Services starts the app without this shell's environment; open(1)
    # passes only what is listed here.
    if [ -n "${LAB_ARCHIVE_URL:-}" ]; then
        set -- "$@" --env "WGSEXTRACT_ARCHIVE_URL=$CLI_ARCHIVE"
    fi
    open -n -a "$APP" "$@"
}

app_setup() {
    events="$R/setup-events.ndjson"
    rm -f "$events"
    launch_app --env GUI_FOR_CLI_AUTO_RUN_SETUP=1 --env "GUI_FOR_CLI_SETUP_EVENTS_LOG=$events" \
        --stdout "$R/app-stdout.txt" --stderr "$R/app-stderr.txt" || return 1
    started=$(date +%s)
    while ! grep -q '"type":"complete"' "$events" 2>/dev/null; do
        if ! app_running; then echo "the app exited during setup"; return 1; fi
        if [ ! -s "$events" ] && [ $(( $(date +%s) - started )) -gt 1200 ]; then
            echo "setup did not start within 20 minutes"; return 1
        fi
        if [ $(( $(date +%s) - started )) -gt 10800 ]; then echo "setup did not finish within 3 hours"; return 1; fi
        sleep 15
    done
    screencapture -x "$R/app-window.png" 2>/dev/null || true
    /usr/bin/python3 - "$events" "$R/setup-result.json" <<'EOF'
import json, sys
results = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
complete = [e for e in results if e.get("type") == "complete"][-1]
json.dump(complete["result"], open(sys.argv[2], "w"), indent=2)
print("setup:", complete["result"]["status"])
EOF
    # Quitting the app (Cmd-Q): it ends its backend and everything it started.
    osascript -e "quit app \"$APP\"" || pkill -TERM -f "$APP/Contents/MacOS/"
    wait_for_app_exit 60
}

app_actions() {
    GUI_FOR_CLI_APP_SUPPORT_NAME="$BUNDLE_ID" "$RESOURCES/node/bin/node" \
        "$RESOURCES/app/dist/web/src/server/bundle-test-cli.js" \
        --plan "$HERE/gui-plan.json" --workspace "$WORKSPACE" \
        --report "$R/bundle-report.json" --log "$R/bundle-test.log" \
        "$RESOURCES/examples/EmbeddedBundle"
    rc=$?
    # The oracle reads outputs where the CLI lab keeps them.
    if [ -d "$WORKSPACE/lab-output" ]; then
        cp -R "$WORKSPACE/lab-output/." "$R/workload/"
    fi
    mkdir -p "$R/workload/info"
    # info has no --outdir; it writes to the configured output directory.
    find "$WORKSPACE/output" "$WORKSPACE/genomes" -name '*.wgse_info.json' \
        -exec cp {} "$R/workload/info/" \; 2>/dev/null
    return $rc
}

app_uninstall() {
    rm -f "$R/uninstall-result.json"
    launch_app --env GUI_FOR_CLI_AUTO_RUN_UNINSTALL=1 \
        --env "GUI_FOR_CLI_UNINSTALL_RESULT_FILE=$R/uninstall-result.json" \
        --stdout "$R/app-uninstall-stdout.txt" --stderr "$R/app-uninstall-stdout.txt" || return 1
    wait_for_file "$R/uninstall-result.json" 600 || return 1
    cat "$R/uninstall-result.json"
    wait_for_app_exit 60 || return 1
    # The app's last cleanup (webview storage it can write back while open)
    # runs just after it exits.
    sleep 5
    # The user drags the app to the Trash (unless the app moved itself), and
    # empties it.
    if [ -e "$APP" ]; then mv "$APP" "$HOME/.Trash/"; fi
    rm -rf "$HOME/.Trash/WGSExtract.app"
    [ ! -e "$APP" ]
}

snapshot baseline
for tool in pixi samtools bcftools java brew conda mamba; do
    found="$(command -v "$tool" 2>/dev/null)"
    if [ "$tool" = java ] && [ -n "$found" ] && ! java -version >/dev/null 2>&1; then
        found=""
    fi
    printf '%s\t%s\n' "$tool" "$found"
done > "$R/preexisting-tools.txt"
# Installers reuse a Pixi in its default place even when PATH doesn't reach it.
pixi_home="$HOME/.pixi/bin/pixi"
[ -e "$pixi_home" ] || pixi_home=""
printf 'pixi-home\t%s\n' "$pixi_home" >> "$R/preexisting-tools.txt"
if [ "$(uname -s)" = Darwin ]; then
    spctl --status > "$R/gatekeeper-status.txt" 2>&1
    /usr/bin/python3 "$HERE/macos-policy.py" > "$R/gatekeeper-policy.json" \
        2> "$R/gatekeeper-policy.log"
fi
if [ "$MODE" = "control" ]; then
    # A different native app with editable text (TextEdit, which every Mac
    # has) is open for 4 minutes: what macOS keeps for any such app.
    printf 'WGS Extract install lab control\n' > "$R/control.txt"
    open -n -a TextEdit "$R/control.txt" > "$R/control-window.log" 2>&1
    sleep 120
    # The control's evidence that the app really ran (lab.py checks it).
    pgrep -x TextEdit > /dev/null && echo "page loaded: True" >> "$R/control-window.log"
    snapshot after_install
    sleep 120
    pkill -x TextEdit >> "$R/control-window.log" 2>&1
    sleep 5
    snapshot after_uninstall
    echo "scenario finished"
    exit 0
fi

if [ -n "${LAB_ARCHIVE_URL:-}" ]; then
    curl -fsSL -o "$CLI_ARCHIVE" "$LAB_ARCHIVE_URL"
fi
step download_dmg download_dmg
step install_app install_app
step gatekeeper gatekeeper
approve_first_launch
step setup app_setup
step actions app_actions
snapshot after_install
if [ "$SABOTAGE" != "skip-uninstall" ]; then
    step uninstall app_uninstall
fi
# Remove what the test itself created, so any remaining change is the product's.
rm -f "$DMG" "$CLI_ARCHIVE"
snapshot after_uninstall
# What the snapshot's preferences file holds (the oracle excuses it only if
# empty): cfprefsd writes it back up to seconds after the app exits, so it is
# read here, after the snapshot, not when the app exits.
preferences="$HOME/Library/Preferences/$BUNDLE_ID.plist"
if [ -f "$preferences" ]; then
    cp "$preferences" "$R/uninstall-preferences.plist"
    plutil -p "$preferences"
fi
# Crash reports written during the run, for the host to read (the oracle fails
# the run on any; the VM and its logs are gone afterwards).
if [ -d "$HOME/Library/Logs/DiagnosticReports" ]; then
    mkdir -p "$R/crash-reports"
    find "$HOME/Library/Logs/DiagnosticReports" -newer "$R/nonce" -type f \
        \( -name '*.ips' -o -name '*.crash' \) -exec cp {} "$R/crash-reports/" \;
fi
echo "scenario finished"
