#!/bin/sh
# shellcheck disable=SC1007
# Install-lab guest scenario for the WGS Extract macOS app.
#
# baseline snapshot -> download the DMG like a browser -> Gatekeeper ->
# drag-install to /Applications -> the app runs its own setup -> real actions
# through the app's bundle and runtime -> snapshot -> the app's own uninstall
# -> empty the Trash -> snapshot
#
# Inputs (environment), besides LAB_RESULTS / LAB_NONCE / LAB_MODE / LAB_SABOTAGE
# (see scenario.sh):
#   LAB_DMG_URL          the DMG under test
#   LAB_RUNNER_URL       tar.gz of the gui-for-cli bundle-test runner
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
BUNDLE_ID="dev.guiforcli.embed.wgsextract"
WORKSPACE="$HOME/Library/Application Support/$BUNDLE_ID/BundleWorkspaces/wgs-extract"
# The test runner is lab tooling, not the product: it lives outside $HOME and
# keeps its own app data under a name the product never uses.
RUNNER_DIR="$R/runner"
# macOS's Local Network privacy keeps an app from reaching the lab host, so the
# CLI tree under test reaches setup as a local file (users' setup downloads
# the pinned release from GitHub instead).
CLI_ARCHIVE="/tmp/wgse-lab-cli.tar.gz"
export GUI_FOR_CLI_APP_SUPPORT_NAME_RUNNER="lab-bundle-runner"

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
    rm -f "$R/setup-result.json"
    launch_app --env GUI_FOR_CLI_AUTO_RUN_SETUP=1 --env GUI_FOR_CLI_QUIT_AFTER_SETUP=1 \
        --env "GUI_FOR_CLI_TERMINAL_LOG_FILE=$R/app-setup-terminal.log" \
        --env "GUI_FOR_CLI_SETUP_RESULT_FILE=$R/setup-result.json" \
        --env GFC_BENCHMARK_STARTUP=1 \
        --env "GFC_BENCHMARK_OUTPUT=$R/startup.txt" || return 1
    wait_for_file "$R/startup.txt" 120 || return 1
    screencapture -x "$R/app-window.png" 2>/dev/null || true
    wait_for_file "$R/setup-result.json" 7200 || return 1
    cat "$R/setup-result.json"
    wait_for_app_exit 120
}

install_runner() {
    mkdir -p "$RUNNER_DIR" &&
        curl -fsSL "$LAB_RUNNER_URL" | tar -xzf - -C "$RUNNER_DIR"
}

app_actions() {
    bundle="$APP/Contents/Resources/GUIForCLIShared_GUIForCLICore.bundle/Contents/Resources/Resources/DemoBundles/WGSExtract"
    GUI_FOR_CLI_APP_SUPPORT_NAME="$GUI_FOR_CLI_APP_SUPPORT_NAME_RUNNER" \
        "$RUNNER_DIR/gui-for-cli" bundle test \
        --plan "$HERE/gui-plan.json" \
        --workspace "$WORKSPACE" \
        --report "$R/bundle-report.json" \
        --log "$R/bundle-test.log" \
        "$bundle"
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
        --env "GUI_FOR_CLI_TERMINAL_LOG_FILE=$R/app-uninstall-terminal.log" \
        --env "GUI_FOR_CLI_UNINSTALL_RESULT_FILE=$R/uninstall-result.json" || return 1
    wait_for_file "$R/uninstall-result.json" 600 || return 1
    cat "$R/uninstall-result.json"
    wait_for_app_exit 60 || return 1
    preferences="$HOME/Library/Preferences/$BUNDLE_ID.plist"
    if [ -f "$preferences" ]; then
        cp "$preferences" "$R/uninstall-preferences.plist" || return 1
        plutil -p "$preferences"
    fi
    # The user empties the Trash.
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
if [ "$(uname -s)" = Darwin ]; then
    spctl --status > "$R/gatekeeper-status.txt" 2>&1
    /usr/bin/python3 "$HERE/macos-policy.py" > "$R/gatekeeper-policy.json" \
        2> "$R/gatekeeper-policy.log"
fi
if [ "$MODE" = "control" ]; then
    sleep 60
    snapshot after_install
    sleep 60
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
step runner install_runner
step actions app_actions
snapshot after_install
if [ "$SABOTAGE" != "skip-uninstall" ]; then
    step uninstall app_uninstall
fi
# Remove what the test itself created, so any remaining change is the product's.
rm -f "$DMG" "$CLI_ARCHIVE"
rm -rf "$RUNNER_DIR" "$HOME/Library/Application Support/$GUI_FOR_CLI_APP_SUPPORT_NAME_RUNNER"
snapshot after_uninstall
# Crash reports written during the run, for the host to read (the oracle fails
# the run on any; the VM and its logs are gone afterwards).
if [ -d "$HOME/Library/Logs/DiagnosticReports" ]; then
    mkdir -p "$R/crash-reports"
    find "$HOME/Library/Logs/DiagnosticReports" -newer "$R/nonce" -type f \
        \( -name '*.ips' -o -name '*.crash' \) -exec cp {} "$R/crash-reports/" \;
fi
echo "scenario finished"
