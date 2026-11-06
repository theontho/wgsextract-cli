#!/bin/sh
# shellcheck disable=SC1007
# Install-lab guest scenario for the WGS Extract Linux app (Tauri, .deb).
#
# baseline snapshot -> download the .deb -> `sudo apt install` it -> the app
# runs its own setup on a virtual display -> real actions through the app's
# bundled runtime -> snapshot -> `sudo apt remove` -> snapshot
#
# Inputs (environment), besides LAB_RESULTS / LAB_NONCE / LAB_MODE (see scenario.sh):
#   LAB_GUI_INSTALLER_URL / LAB_GUI_INSTALLER_NAME   the .deb under test
#   LAB_CLI_ARCHIVE_URL   optional CLI source archive for setup to install
#                         (unset: setup installs the CLI release the app pins)
# The user is an Ubuntu desktop user with sudo (as Ubuntu's first user is);
# Xvfb stands in for their display. Nothing here decides pass/fail.
set -u

R="${LAB_RESULTS:?}"
HERE="$(CDPATH= cd "$(dirname "$0")" && pwd)"
EVIDENCE="$R/evidence"
mkdir -p "$EVIDENCE" "$R/workload"
printf '%s\n' "${LAB_NONCE:?}" > "$R/nonce"
MODE="${LAB_MODE:-install}"
DEB="$HOME/Downloads/${LAB_GUI_INSTALLER_NAME:-WGSExtract.deb}"
PACKAGE="wgs-extract"
APP_DIR="/usr/lib/WGSExtract"
APP_BIN="/usr/bin/gui-for-cli-webui-tauri"
APP_ID="dev.guiforcli.web.embed.wgsextract"
WORKSPACE="$HOME/.local/share/$APP_ID/BundleWorkspaces/wgs-extract"
# A desktop session's runtime folder (logind's /run/user/<uid>), where dconf
# keeps its runtime state; without one it falls back to ~/.cache/dconf.
XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)"
export XDG_RUNTIME_DIR
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"

# What happened when, in milliseconds: the oracle tells from it what stopped a
# setup step (gui_oracle.py).
mark() {
    printf '%s\t%s\n' "$(date +%s%3N)" "$*" >> "$R/timeline.tsv"
}

step() {
    name="$1"
    shift
    start=$(date +%s)
    mark "step-start $name"
    "$@" > "$EVIDENCE/$name.log" 2>&1
    rc=$?
    mark "step-end $name $rc"
    echo "$rc" > "$EVIDENCE/$name.rc"
    echo $(( $(date +%s) - start )) > "$EVIDENCE/$name.seconds"
    echo "scenario step $name rc=$rc"
}

snapshot() {
    sh "$HERE/inventory.sh" "$R/inventory-$1.txt" "$R"
}

# The app and everything it started: its backend, and setup's scripts and
# downloads, which run from the workspace.
app_pids() {
    pgrep -u "$(id -u)" -f "$APP_DIR/|$APP_BIN|$WORKSPACE/" || true
}

download_installer() {
    mkdir -p "$HOME/Downloads" && curl -fsSL -o "$DEB" "$LAB_GUI_INSTALLER_URL"
}

install_app() {
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$DEB" &&
        dpkg -s "$PACKAGE" && test -x "$APP_BIN" &&
        test -f /usr/share/applications/WGSExtract.desktop
}

# The app this scenario started (its xvfb-run), by process ID. Matching
# command lines misses it until the background shell has exec'd it (#448).
app_alive() {
    kill -0 "$app_pid" 2>/dev/null &&
        ! grep -q '^State:[[:space:]]*Z' "/proc/$app_pid/status" 2>/dev/null
}

# Notes when the setup event log grows, so the oracle can tell what came
# before each event.
note_events() {
    lines=0
    [ -f "$events" ] && lines=$(wc -l < "$events")
    [ "$lines" = "$seen" ] || { seen=$lines; mark "setup-events $lines"; }
}

# Waits up to $1 seconds for the app and everything it started to be gone.
app_gone() {
    for _ in $(seq "$1"); do
        if ! app_alive && [ -z "$(app_pids)" ]; then
            if [ -z "${app_rc:-}" ]; then
                wait "$app_pid"
                app_rc=$?
                mark "app-exited $app_rc"
            fi
            note_events
            mark "app-closed"
            return 0
        fi
        sleep 1
    done
    return 1
}

# Closing the window: the app ends its backend and everything it started.
# Every way out of setup closes the app, so no later step runs beside its
# setup; the app's Uninstall stops whatever runs from the runtime (#448).
# What won't stop ends the scenario: its missing evidence fails the verdict.
close_app() {
    mark "harness-closes-app $1"
    pkill -TERM -u "$(id -u)" -f "^$APP_BIN" || true
    # Without the app, nothing else stops what it started.
    app_alive || pkill -TERM -u "$(id -u)" -f "$APP_DIR/|$WORKSPACE/" || true
    app_gone 60 && return 0
    echo "still running 60 s after the window closed: $(app_pids)"
    mark "harness-kills-app still running 60 s after the window closed"
    pkill -KILL -u "$(id -u)" -f "$APP_DIR/|$APP_BIN|$WORKSPACE/" || true
    app_gone 10 && return 1
    echo "could not stop: $(app_pids)"
    mark "harness-aborts could not stop the app"
    exit 1
}

app_setup() {
    events="$R/setup-events.ndjson"
    # Opening the app; it runs its own setup.
    env GUI_FOR_CLI_AUTO_RUN_SETUP=1 GUI_FOR_CLI_SETUP_EVENTS_LOG="$events" \
        ${LAB_CLI_ARCHIVE_URL:+WGSEXTRACT_ARCHIVE_URL="$LAB_CLI_ARCHIVE_URL"} \
        xvfb-run -a -s "-screen 0 1280x800x24" "$APP_BIN" > "$R/app-stdout.txt" 2> "$R/app-stderr.txt" &
    app_pid=$!
    mark "app-launched $app_pid"
    started=$(date +%s)
    seen=0
    while :; do
        note_events
        grep -q '"type":"complete"' "$events" 2>/dev/null && { note_events; break; }
        if ! app_alive; then
            wait "$app_pid"
            app_rc=$?
            mark "app-exited $app_rc"
            echo "the app exited during setup (exit status $app_rc)"
            # What it started may outlive it.
            close_app "the app exited during setup"
            return 1
        fi
        if [ ! -s "$events" ] && [ $(( $(date +%s) - started )) -gt 1200 ]; then
            echo "setup did not start within 20 minutes"
            close_app "setup did not start within 20 minutes"
            return 1
        fi
        if [ $(( $(date +%s) - started )) -gt 10800 ]; then
            echo "setup did not finish within 3 hours"
            close_app "setup did not finish within 3 hours"
            return 1
        fi
        sleep 2
    done
    python3 - "$events" "$R/setup-result.json" <<'EOF'
import json, sys
results = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
complete = [e for e in results if e.get("type") == "complete"][-1]
json.dump(complete["result"], open(sys.argv[2], "w"), indent=2)
print("setup:", complete["result"]["status"])
EOF
    close_app "setup complete"
}

app_actions() {
    runner="$APP_DIR/app/dist/web/src/server/bundle-test-cli.js"
    # Released apps before the single-app layout use platform/typescript.
    if [ ! -f "$runner" ]; then
        runner="$APP_DIR/platform/typescript/dist/web/src/server/bundle-test-cli.js"
    fi
    GUI_FOR_CLI_APP_SUPPORT_NAME="$APP_ID" "$APP_DIR/node/bin/node" \
        "$runner" \
        --plan "$HERE/gui-plan.json" --workspace "$WORKSPACE" \
        --report "$R/bundle-report.json" --log "$R/bundle-test.log" \
        "$APP_DIR/examples/EmbeddedBundle"
    rc=$?
    if [ -d "$WORKSPACE/lab-output" ]; then
        cp -R "$WORKSPACE/lab-output/." "$R/workload/"
    fi
    mkdir -p "$R/workload/info"
    find "$WORKSPACE/output" "$WORKSPACE/genomes" -name '*.wgse_info.json' \
        -exec cp {} "$R/workload/info/" \; 2>/dev/null
    return $rc
}

app_uninstall() {
    # The app's own Uninstall (File > Uninstall WGSExtract...; the package
    # manager can't remove what setup put in the home folder), which quits the app...
    env GUI_FOR_CLI_AUTO_RUN_UNINSTALL=1 GUI_FOR_CLI_UNINSTALL_RESULT_FILE="$R/uninstall-result.json" \
        xvfb-run -a -s "-screen 0 1280x800x24" "$APP_BIN" > "$R/app-uninstall-stdout.txt" 2>&1
    rc=$?
    cat "$R/uninstall-result.json"
    [ "$rc" = 0 ] || { echo "the app's Uninstall exited $rc"; return 1; }
    # ...then removing the package, and what apt installed only for it.
    sudo DEBIAN_FRONTEND=noninteractive apt-get remove --autoremove -y -q "$PACKAGE" &&
        ! dpkg -s "$PACKAGE" 2>/dev/null
}

# A desktop user's home has the XDG folders their session made.
mkdir -p "$HOME/Downloads" "$HOME/.local/share" "$HOME/.cache" "$HOME/.config"
snapshot baseline
for tool in pixi samtools bcftools java conda mamba; do
    printf '%s\t%s\n' "$tool" "$(command -v "$tool" 2>/dev/null)"
done > "$R/preexisting-tools.txt"
# Installers reuse a Pixi in its default place even when PATH doesn't reach it.
pixi_home="$HOME/.pixi/bin/pixi"
[ -e "$pixi_home" ] || pixi_home=""
printf 'pixi-home\t%s\n' "$pixi_home" >> "$R/preexisting-tools.txt"
if [ "$MODE" = "control" ]; then
    # A different WebKitGTK app (webkit-window.py) is installed, open for 4
    # minutes and removed: what GTK, WebKit and Mesa keep for any such app.
    set -- gir1.2-webkit2-4.1 python3-gi
    # The log is the user's own file; only apt-get needs root.
    # shellcheck disable=SC2024
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" > "$R/control-apt.log" 2>&1
    xvfb-run -a -s "-screen 0 1280x800x24" python3 "$HERE/webkit-window.py" 240 > "$R/control-window.log" 2>&1
    # shellcheck disable=SC2024
    sudo DEBIAN_FRONTEND=noninteractive apt-get remove --autoremove -y -q "$@" >> "$R/control-apt.log" 2>&1
    snapshot after_install
    snapshot after_uninstall
    echo "scenario finished"
    exit 0
fi

step download_installer download_installer
step install_app install_app
step setup app_setup
step optional_tools sh "$HERE/install-optional-tools.sh" "$HOME/.local/share/$APP_ID"
step actions app_actions
snapshot after_install
step uninstall app_uninstall
rm -f "$DEB"
snapshot after_uninstall
echo "scenario finished"
