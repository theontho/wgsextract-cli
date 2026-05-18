#!/bin/bash

# Load common functions
# shellcheck source=/dev/null
source "$(dirname "$0")/../common.sh"

if [[ "$1" == "--describe" ]]; then
    echo "Description: Verifies the CLI's internal fallback to Pixi environments when tools are missing from the system PATH."
    echo "✅ Verified End Goal: Successful detection and execution of a tool using Pixi fallback; confirmed by Python check resolving and running a hidden Pixi-managed tool through 'pixi run' with a cleaned PATH."
    exit 0
fi

OUTDIR="out/smoke_test_pixi_fallback"
mkdir -p "$OUTDIR"

echo "--------------------------------------------------------"
echo "  WGS Extract CLI: Pixi Fallback Smoke Test"
echo "--------------------------------------------------------"

# 1. Create a clean PATH environment that definitely DOES NOT have the selected tool
# We save the original path first
ORIG_PATH="$PATH"
PIXI_PATH=$(dirname "$(which pixi)")
MINIMAL_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
FALLBACK_TOOL=""
for candidate in freebayes delly haplogrep bgzip; do
    if ! PATH="$MINIMAL_PATH" command -v "$candidate" &> /dev/null; then
        FALLBACK_TOOL="$candidate"
        break
    fi
done

if [ -z "$FALLBACK_TOOL" ]; then
    echo "⏭️ SKIP: No Pixi-managed fallback test tool is hidden by the minimal PATH."
    exit 77
fi

export PATH="$MINIMAL_PATH"

echo ":: Verifying '$FALLBACK_TOOL' is NOT in the current PATH..."
if command -v "$FALLBACK_TOOL" &> /dev/null; then
    echo "❌ Error: '$FALLBACK_TOOL' found in path even after clearing it! Path: $(command -v "$FALLBACK_TOOL")"
    exit 1
else
    echo "✅ Success: '$FALLBACK_TOOL' is hidden from the system."
fi

echo ":: Checking if the CLI can still resolve $FALLBACK_TOOL via Pixi..."
export PATH="$ORIG_PATH"
CLEAN_PATH="$PIXI_PATH:$MINIMAL_PATH"

FALLBACK_TOOL="$FALLBACK_TOOL" CLEAN_PATH="$CLEAN_PATH" pixi run python3 -c "
import os
import shlex
import subprocess
from wgsextract_cli.core.dependencies import get_tool_path
tool = os.environ['FALLBACK_TOOL']
os.environ['PATH'] = os.environ['CLEAN_PATH']
path = get_tool_path(tool)
print(f'Resolved {tool} to: {path}')
if not path or f'pixi run -e default {tool}' not in path:
    raise SystemExit(f'CLI did not resolve {tool} through Pixi fallback')
subprocess.run(shlex.split(path) + ['--version'], check=True, stdout=subprocess.DEVNULL)
print(f'✅ SUCCESS: CLI correctly fell back to Pixi for {tool}')
" > "$OUTDIR/python_check.stdout" 2>&1

if grep -q "SUCCESS" "$OUTDIR/python_check.stdout"; then
    cat "$OUTDIR/python_check.stdout"
else
    echo "❌ FAILURE: CLI could not resolve $FALLBACK_TOOL through Pixi fallback."
    cat "$OUTDIR/python_check.stdout"
    exit 1
fi

echo ""
echo "========================================================"
echo "Pixi Fallback Smoke Test: PASSED"
echo "========================================================"
