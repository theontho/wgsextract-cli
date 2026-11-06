# shellcheck shell=sh disable=SC2154
# Watching and closing the app that runs setup, for the Linux and macOS
# scenarios (sourced). The scenario sets:
#   R           the results folder
#   APP_MAIN    an ERE for the app's main process (what closing its window ends)
#   APP_TREE    an ERE for everything the app runs: the app, its backend, and
#               setup's scripts and downloads under the workspace
#   app_pid     the launched app's PID, or empty when it was launched through
#               the OS (open -a) and is known only by APP_MAIN
#   events      setup's event log
# Every way out of setup closes the app and waits until nothing it started is
# left, so no later step runs beside setup: the app's Uninstall stops every
# process under the runtime folder, a running install included (#448).

now_ms() {
    # BSD date has no %N.
    if [ "$(uname -s)" = Darwin ]; then
        /usr/bin/python3 -c 'import time; print(int(time.time() * 1000))'
    else
        date +%s%3N
    fi
}

# What happened when: the oracle tells from it what stopped a setup step, and
# whether anything ran beside setup (gui_oracle.py).
mark() {
    printf '%s\t%s\n' "$(now_ms)" "$*" >> "$R/timeline.tsv"
}

app_pids() {
    pgrep -u "$(id -u)" -f "$APP_TREE" || true
}

# The launched app, by its PID: matching command lines misses it until the
# background shell has exec'd it (#448). A zombie has exited.
app_alive() {
    if [ -z "${app_pid:-}" ]; then
        pgrep -u "$(id -u)" -f "$APP_MAIN" > /dev/null
        return
    fi
    kill -0 "$app_pid" 2>/dev/null &&
        ! ps -o stat= -p "$app_pid" 2>/dev/null | grep -q '^Z'
}

app_launched() {
    app_rc=""
    seen=0
    mark "app-launched ${app_pid:-by-name}"
}

# Notes when setup's event log grows, so the oracle can tell what came before
# each event.
note_events() {
    lines=0
    [ -f "$events" ] && lines=$(wc -l < "$events" | tr -d ' ')
    [ "$lines" = "$seen" ] || { seen=$lines; mark "setup-events $lines"; }
}

# Notes the app's exit (once), and its exit status when it is this shell's child.
reap_app() {
    [ -z "$app_rc" ] || return 0
    if [ -n "${app_pid:-}" ]; then
        wait "$app_pid"
        app_rc=$?
    else
        app_rc=unknown
    fi
    mark "app-exited $app_rc"
    # Events the app wrote as it went count as before anything the harness does.
    note_events
}

# Waits up to $1 seconds for the app and everything it started to be gone.
app_gone() {
    for _ in $(seq "$1"); do
        if ! app_alive && [ -z "$(app_pids)" ]; then
            reap_app
            note_events
            mark "app-closed"
            return 0
        fi
        sleep 1
    done
    return 1
}

# Closing the window ends the app's backend and what it runs. Without the app,
# nothing else stops what it started. What won't stop ends the scenario: the
# evidence it then lacks fails the verdict.
close_app() {
    mark "harness-closes-app $1"
    pkill -TERM -u "$(id -u)" -f "$APP_MAIN" || true
    app_alive || pkill -TERM -u "$(id -u)" -f "$APP_TREE" || true
    app_gone 60 && return 0
    echo "still running 60 s after the window closed: $(app_pids)"
    mark "harness-kills-app still running 60 s after the window closed"
    pkill -KILL -u "$(id -u)" -f "$APP_TREE" || true
    app_gone 10 && return 1
    echo "could not stop: $(app_pids)"
    mark "harness-aborts could not stop the app"
    exit 1
}

# Waits up to $1 seconds for setup's complete event while the app runs. On
# any other outcome it says why, closes the app and returns 1.
await_setup() {
    started=$(date +%s)
    while :; do
        note_events
        grep -q '"type":"complete"' "$events" 2>/dev/null && { note_events; return 0; }
        if ! app_alive; then
            reap_app
            echo "the app exited during setup (exit status $app_rc)"
            close_app "the app exited during setup"
            return 1
        fi
        if [ ! -s "$events" ] && [ $(( $(date +%s) - started )) -gt 1200 ]; then
            echo "setup did not start within 20 minutes"
            close_app "setup did not start within 20 minutes"
            return 1
        fi
        if [ $(( $(date +%s) - started )) -gt "$1" ]; then
            echo "setup did not finish within $1 s"
            close_app "setup did not finish within $1 s"
            return 1
        fi
        sleep 2
    done
}
