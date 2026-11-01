#!/bin/bash
set -euo pipefail
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != aarch64 ]; then
    echo "Build on native Linux aarch64, not an x86 host or emulation." >&2
    exit 1
fi
for tool in docker git python3; do
    command -v "$tool" >/dev/null || { echo "Install $tool before building." >&2; exit 1; }
done
case "$(docker info --format '{{.Architecture}}')" in
    aarch64|arm64) ;;
    *) echo "A native ARM64 Docker engine is required." >&2; exit 1 ;;
esac
recipe="$(cd "$(dirname "$0")" && pwd)"
work="${1:-out/deepvariant-arm64-build}"
mkdir -p "$work"
work="$(cd "$work" && pwd)"
if [ -e "$work/source" ]; then
    echo "Use an empty build directory; $work/source already exists." >&2
    exit 1
fi
git clone --depth 1 --branch v1.10.0-arm64.1 \
    https://github.com/Lambda-Biolab/deepvariant-linux-arm64.git "$work/source"
cd "$work/source"
test "$(git rev-parse HEAD)" = ff2a2a949c8dd244908499ceb6a9fda381fcccf6
git fetch --depth 1 https://github.com/google/deepvariant.git refs/tags/v1.10.0
test "$(git rev-parse FETCH_HEAD)" = d6b15e21323d9d15b347083f1d256ad62998c4cf
# Retain upstream's algorithms and model code, not the fork's accuracy trade-offs.
git restore --source FETCH_HEAD -- deepvariant third_party scripts/run_deepvariant.py
git restore --source HEAD -- third_party/htslib.BUILD third_party/libssw.BUILD
git diff FETCH_HEAD -- deepvariant third_party scripts/run_deepvariant.py \
    > "$work/scientific-source.patch"
sed -i 's|git clone https://github.com/tensorflow/tensorflow|git clone --depth 1 --branch "${DV_CPP_TENSORFLOW_TAG}" https://github.com/tensorflow/tensorflow|' \
    scripts/build/build-prereq-arm64.sh
python3 - <<'PY'
from pathlib import Path
p = Path("scripts/build/build_release_binaries_arm64.sh")
s = p.read_text().replace("/tmp/", "/opt/deepvariant/build-tmp/")
s = s.replace(
    "TMPDIR=$(mktemp -d -t tmp.XXXXXXXXXXX)",
    'TMPDIR="/opt/deepvariant/build-tmp/zip-$(printf %s "$orig_zip_file" | tr / -)"; rm -rf "$TMPDIR"; mkdir -p "$TMPDIR"',
)
p.write_text(s)
PY
printf 'build --jobs=12\nbuild --local_ram_resources=49152\nbuild --cxxopt=-include\nbuild --cxxopt=cstdint\nbuild --host_cxxopt=-include\nbuild --host_cxxopt=cstdint\n' \
    > user.bazelrc
docker build -f "$recipe/Builder.Dockerfile" \
    -t wgsextract/deepvariant-builder:1.10.0-arm64 .
cp "$recipe/model-sha256.txt" model-sha256.txt
docker build -f "$recipe/Runtime.Dockerfile" \
    --build-arg BUILDER_IMAGE=wgsextract/deepvariant-builder:1.10.0-arm64 \
    -t wgsextract/deepvariant:1.10.0-arm64 .
test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' \
    wgsextract/deepvariant:1.10.0-arm64)" = linux/arm64
echo "Built wgsextract/deepvariant:1.10.0-arm64; no emulation or quantized model."
