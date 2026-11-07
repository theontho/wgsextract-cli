#!/bin/sh
# Verifies Xcode Command Line Tools, installing them when missing. Checking
# needs no privileges. As root (headless or CI runs) the tools are installed
# with softwareupdate; otherwise the macOS installer dialog is opened, which
# asks for an administrator's approval itself, and the script waits for it.
set -eu

if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' "Xcode Command Line Tools are only required on macOS."
  exit 0
fi

tools_ready() {
  developer_dir="$(/usr/bin/xcode-select -p 2>/dev/null)" || return 1
  [ -d "$developer_dir" ] && /usr/bin/xcrun --find clang >/dev/null 2>&1
}

report_ready() {
  printf 'Xcode Command Line Tools are installed: %s\n' "$developer_dir"
  exit 0
}

run_logged() {
  log_file="$1"
  description="$2"
  max_wait_seconds="$3"
  shift 3
  : >"$log_file"
  "$@" >"$log_file" 2>&1 &
  command_pid=$!
  elapsed_seconds=0
  while /bin/kill -0 "$command_pid" >/dev/null 2>&1; do
    sleep 10
    elapsed_seconds=$((elapsed_seconds + 10))
    printf 'Still %s (%s seconds elapsed)...\n' "$description" "$elapsed_seconds"
    /usr/bin/tail -n 5 "$log_file" | /usr/bin/sed 's/^/[softwareupdate] /'
    if [ "$elapsed_seconds" -ge "$max_wait_seconds" ]; then
      /bin/kill "$command_pid" >/dev/null 2>&1 || true
      sleep 5
      /bin/kill -0 "$command_pid" >/dev/null 2>&1 && /bin/kill -9 "$command_pid" >/dev/null 2>&1 || true
      wait "$command_pid" >/dev/null 2>&1 || true
      /bin/cat "$log_file"
      printf 'Timed out while %s after %s seconds.\n' "$description" "$elapsed_seconds" >&2
      return 124
    fi
  done
  command_status=0
  wait "$command_pid" || command_status=$?
  /bin/cat "$log_file"
  return "$command_status"
}

# Installs the newest Command Line Tools package listed by softwareupdate.
# Returns 1 when none is listed, so the caller can fall back to the dialog.
install_with_softwareupdate() {
  install_marker="/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
  list_log="$(/usr/bin/mktemp "${TMPDIR:-/tmp}/wgsextract-clt-list.XXXXXX")"
  install_log="$(/usr/bin/mktemp "${TMPDIR:-/tmp}/wgsextract-clt-install.XXXXXX")"
  trap '/bin/rm -f "$list_log" "$install_log" "$install_marker"' EXIT
  # The marker makes softwareupdate list the on-demand Command Line Tools package.
  /usr/bin/touch "$install_marker"

  printf '%s\n' "Checking Apple's Software Update catalog for Xcode Command Line Tools..."
  label=""
  if run_logged "$list_log" "checking Apple's Software Update catalog" "${SOFTWAREUPDATE_LIST_TIMEOUT_SECONDS:-900}" \
    /usr/sbin/softwareupdate --list; then
    label="$(
      /usr/bin/awk '
        /\* Label: Command Line Tools/ { sub(/^.*Label: /, ""); print }
        /\* Command Line Tools/ { sub(/^.*\* /, ""); print }
      ' "$list_log" | /usr/bin/tail -n 1
    )"
  fi
  [ -n "$label" ] || return 1

  printf 'Installing Xcode Command Line Tools package: %s\n' "$label"
  if ! run_logged "$install_log" "installing Xcode Command Line Tools" "${SOFTWAREUPDATE_INSTALL_TIMEOUT_SECONDS:-1800}" \
    /usr/sbin/softwareupdate --install "$label" --verbose; then
    printf '%s\n' "Failed to install Xcode Command Line Tools with softwareupdate; check the output above, then rerun setup." >&2
    exit 1
  fi
}

open_installer_dialog() {
  if /usr/bin/xcode-select --install >/dev/null 2>&1; then
    printf '%s\n' "Opened the macOS installer for Xcode Command Line Tools."
  else
    printf '%s\n' "The macOS installer for Xcode Command Line Tools may already be open."
  fi
  cat <<'EOF'
Click "Install" in the dialog and finish the installation. An administrator's
name and password may be needed. Setup continues automatically once the tools
are installed.
EOF
}

if tools_ready; then
  report_ready
fi

printf '%s\n' "Xcode Command Line Tools are required before WGS Extract setup can continue."
if [ "$(/usr/bin/id -u)" -ne 0 ] || ! install_with_softwareupdate; then
  open_installer_dialog
fi

timeout_seconds="${XCODE_SELECT_INSTALL_TIMEOUT_SECONDS:-1800}"
elapsed_seconds=0
printf '%s\n' "Waiting for Xcode Command Line Tools to become available..."
while [ "$elapsed_seconds" -lt "$timeout_seconds" ]; do
  if tools_ready; then
    report_ready
  fi
  sleep 5
  elapsed_seconds=$((elapsed_seconds + 5))
  if [ $((elapsed_seconds % 60)) -eq 0 ]; then
    printf 'Still waiting for Xcode Command Line Tools (%s seconds elapsed)...\n' "$elapsed_seconds"
  fi
done

cat >&2 <<EOF
Timed out after $timeout_seconds seconds waiting for Xcode Command Line Tools.
Finish the macOS installer (or run \`xcode-select --install\` in Terminal), then
rerun setup.
EOF
exit 1
