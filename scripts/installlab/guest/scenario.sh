#!/bin/sh
# Install-lab guest scenario: what a new user does, from a clean machine.
#
# baseline snapshot -> `curl | sh` install -> snapshot -> real workload ->
# update (reinstall) -> quick re-check -> uninstall -> snapshot
#
# Inputs (environment):
#   LAB_RESULTS          writable directory the host collects (outside $HOME)
#   LAB_NONCE            per-run token the host checks in the evidence
#   LAB_INSTALLER_URL    URL of install.sh to pipe into sh
#   LAB_ARCHIVE_URL      optional source archive URL (testing an unreleased tree)
#   LAB_DATASET_URL      tar.gz of the mini genome dataset, and its
#   LAB_DATASET_SHA256   expected SHA-256
#   LAB_SABOTAGE         optional negative control (see lab.py)
#   LAB_MODE             "install" (default) or "control" (no install; measures
#                        background noise in the state snapshots)
#   LAB_INSTALL_DIR      optional install folder name under $HOME, e.g. one with
#                        a space ("WGS Extract"); default is the README's
#   LAB_UPDATE_FROM      optional published release tag to install first with
#                        its own installer, then update to the tree under test
#
# Nothing here decides pass/fail; the host's oracle does, from the evidence.
set -u

R="${LAB_RESULTS:?}"
HERE="$(CDPATH= cd "$(dirname "$0")" && pwd)"
EVIDENCE="$R/evidence"
mkdir -p "$EVIDENCE"
printf '%s\n' "${LAB_NONCE:?}" > "$R/nonce"
SABOTAGE="${LAB_SABOTAGE:-}"
MODE="${LAB_MODE:-install}"
INSTALL_DIR="$HOME/${LAB_INSTALL_DIR:-wgsextract-cli}"
if [ -n "${LAB_INSTALL_DIR:-}" ]; then
    export WGSEXTRACT_INSTALL_DIR="$INSTALL_DIR"
fi
WGSE="$INSTALL_DIR/wgsextract"
DATA_PARENT="$HOME/lab-data"

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

sha256_of() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

install_cli() {
    if [ "$SABOTAGE" = "noop-installer" ]; then
        return 0
    fi
    # Exactly the README command, run from the directory the user is in.
    cd "$HOME" || return 1
    if [ -n "${LAB_ARCHIVE_URL:-}" ]; then
        curl -fsSL "$LAB_INSTALLER_URL" | WGSEXTRACT_ARCHIVE_URL="$LAB_ARCHIVE_URL" WGSEXTRACT_NO_OPEN=1 sh
    else
        curl -fsSL "$LAB_INSTALLER_URL" | WGSEXTRACT_NO_OPEN=1 sh
    fi
}

fetch_dataset() {
    mkdir -p "$DATA_PARENT" &&
        curl -fsSL -o "$DATA_PARENT/dataset.tar.gz" "$LAB_DATASET_URL" &&
        actual="$(sha256_of "$DATA_PARENT/dataset.tar.gz")" &&
        echo "sha256 $actual" &&
        [ "$actual" = "$LAB_DATASET_SHA256" ] &&
        tar -xzf "$DATA_PARENT/dataset.tar.gz" -C "$DATA_PARENT"
}

apply_post_install_sabotage() {
    case "$SABOTAGE" in
        fake-launcher)
            # A launcher that claims success without doing anything.
            printf '#!/bin/sh\nexit 0\n' > "$WGSE"
            chmod +x "$WGSE"
            ;;
        break-samtools)
            find "$INSTALL_DIR" -path "*/envs/default/bin/samtools" -exec rm -f {} + 2>/dev/null
            ;;
    esac
}

snapshot baseline
# Tools the installer must provide must not be reachable before it runs, or a
# passing run could be relying on them.
for tool in pixi samtools bcftools java brew conda mamba; do
    found="$(command -v "$tool" 2>/dev/null)"
    # macOS ships a /usr/bin/java stub that only says "no Java runtime".
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
fi
if [ "$MODE" = "control" ]; then
    # Same timeline without installing anything: whatever changes is OS noise.
    sleep 60
    snapshot after_install
    sleep 60
    snapshot after_uninstall
    echo "scenario finished"
    exit 0
fi

install_release() {
    # What a user who installed that release ran then: its own installer.
    cd "$HOME" || return 1
    curl -fsSL "https://raw.githubusercontent.com/theontho/wgsextract-cli/$LAB_UPDATE_FROM/install.sh" |
        WGSEXTRACT_RELEASE_TAG="$LAB_UPDATE_FROM" WGSEXTRACT_NO_OPEN=1 sh
}

mark_user_data() {
    # An update must keep user data that lives inside app/.
    mkdir -p "$INSTALL_DIR/app/reference"
    printf '%s\n' "$LAB_NONCE" > "$INSTALL_DIR/app/reference/lab-marker"
}

DATA="$DATA_PARENT/hg19-mini-hg00096"
install_optional_tools() {
    pixi="$INSTALL_DIR/.pixi/bin/pixi"
    if [ ! -x "$pixi" ]; then
        pixi="$(command -v pixi || printf '%s' "$HOME/.pixi/bin/pixi")"
    fi
    PIXI_CACHE_DIR="$INSTALL_DIR/.pixi/cache" \
    RATTLER_CACHE_DIR="$INSTALL_DIR/.pixi/cache/rattler" \
        "$pixi" install --locked --manifest-path "$INSTALL_DIR/app/pixi.toml" -e gatk
}

if [ -n "${LAB_UPDATE_FROM:-}" ]; then
    # Update a real earlier release, then test what the update produced.
    step install install_release
    mark_user_data
    step update install_cli
    snapshot after_install
    step dataset fetch_dataset
    step optional_tools install_optional_tools
    apply_post_install_sabotage
    step workload sh "$HERE/workload.sh" "$WGSE" "$DATA" "$R/workload"
else
    step install install_cli
    snapshot after_install
    step dataset fetch_dataset
    step optional_tools install_optional_tools
    apply_post_install_sabotage
    step workload sh "$HERE/workload.sh" "$WGSE" "$DATA" "$R/workload"
    mark_user_data
    step update install_cli
fi
step update_kept_data cat "$INSTALL_DIR/app/reference/lab-marker"
step post_update_mt "$WGSE" lineage mt-haplogroup --input "$DATA/HG00096.hg19-mini.bam" \
    --ref "$DATA/hg19-mini.fa.gz" --outdir "$R/post_update_mt"

if [ "$SABOTAGE" != "skip-uninstall" ]; then
    step uninstall "$INSTALL_DIR/uninstall.sh" --yes --remove-config --remove-pixi
fi
if [ "$SABOTAGE" = "leftover" ]; then
    mkdir -p "$HOME/.wgsextract-leftover"
fi
# Remove what the test itself created, so any remaining change is the installer's.
rm -rf "$DATA_PARENT"
snapshot after_uninstall
# Crash reports written during the run, for the host to read (the oracle fails
# the run on any; the VM and its logs are gone afterwards).
if [ -d "$HOME/Library/Logs/DiagnosticReports" ]; then
    mkdir -p "$R/crash-reports"
    find "$HOME/Library/Logs/DiagnosticReports" -newer "$R/nonce" -type f \
        \( -name '*.ips' -o -name '*.crash' \) -exec cp {} "$R/crash-reports/" \;
fi
echo "scenario finished"
