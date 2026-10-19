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
INSTALL_DIR="$HOME/wgsextract-cli"
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
if [ "$MODE" = "control" ]; then
    # Same timeline without installing anything: whatever changes is OS noise.
    sleep 60
    snapshot after_install
    sleep 60
    snapshot after_uninstall
    echo "scenario finished"
    exit 0
fi

step install install_cli
snapshot after_install
step dataset fetch_dataset
apply_post_install_sabotage
DATA="$DATA_PARENT/hg19-mini-hg00096"
step workload sh "$HERE/workload.sh" "$WGSE" "$DATA" "$R/workload"

# An update must keep user data that lives inside app/.
mkdir -p "$INSTALL_DIR/app/reference"
printf '%s\n' "$LAB_NONCE" > "$INSTALL_DIR/app/reference/lab-marker"
step update install_cli
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
echo "scenario finished"
