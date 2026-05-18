#!/bin/bash

# Load common functions
# shellcheck source=/dev/null
source "$(dirname "$0")/../common.sh"

if [[ "$1" == "--describe" ]]; then
    echo "Description: Verifies the CLI's internal fallback to Pixi environments when tools are missing from the system PATH."
    echo "✅ Verified End Goal: Successful detection and execution of a tool using Pixi fallback; confirmed by Python check resolving and running samtools through 'pixi run' with a cleaned PATH."
    exit 0
fi

OUTDIR="out/smoke_test_pixi_fallback"
mkdir -p "$OUTDIR"

echo "--------------------------------------------------------"
echo "  WGS Extract CLI: Pixi Fallback Smoke Test"
echo "--------------------------------------------------------"

# 1. Create a clean PATH environment that definitely DOES NOT have samtools
# We save the original path first
ORIG_PATH="$PATH"
# Minimal path
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"

echo ":: Verifying 'samtools' is NOT in the current PATH..."
if command -v samtools &> /dev/null; then
    echo "❌ Error: 'samtools' found in path even after clearing it! Path: $(command -v samtools)"
    exit 1
else
    echo "✅ Success: 'samtools' is hidden from the system."
fi

echo ":: Checking if the CLI can still resolve samtools via Pixi..."
export PATH="$ORIG_PATH"
PIXI_PATH=$(dirname "$(which pixi)")
CLEAN_PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PIXI_PATH"

pixi run python3 -c "
import os
import shlex
import subprocess
from wgsextract_cli.core.dependencies import get_tool_path
os.environ['PATH'] = '$CLEAN_PATH'
path = get_tool_path('samtools')
print(f'Resolved samtools to: {path}')
if not path or 'pixi run -e default samtools' not in path:
    raise SystemExit('CLI did not resolve samtools through Pixi fallback')
subprocess.run(shlex.split(path) + ['--version'], check=True, stdout=subprocess.DEVNULL)
print('✅ SUCCESS: CLI correctly fell back to Pixi for samtools')
" > "$OUTDIR/python_check.stdout" 2>&1

if grep -q "SUCCESS" "$OUTDIR/python_check.stdout"; then
    cat "$OUTDIR/python_check.stdout"
else
    echo "❌ FAILURE: CLI could not resolve samtools through Pixi fallback."
    cat "$OUTDIR/python_check.stdout"
    exit 1
fi

echo ""
echo "========================================================"
echo "Pixi Fallback Smoke Test: PASSED"
echo "========================================================"
