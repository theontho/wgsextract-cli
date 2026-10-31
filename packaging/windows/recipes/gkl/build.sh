source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

BUILD_U=$(cygpath -u "$BUILD_PREFIX")
JNI=$(find "$BUILD_U" -name jni.h -print -quit)
JAR=$(find "$BUILD_U" -name jar.exe -print -quit)
test -n "$JNI" && test -n "$JAR"
JNI=$(dirname "$JNI")
SRC_U=$(cygpath -u "$SRC_DIR")
NATIVE="$SRC_U/src/main/native"
COMMON=(-std=c++11 -O3 -march=x86-64-v2 -mtune=generic -DWGSE_GKL_AVX_ONLY
    "-I$JNI" "-I$JNI/win32" "-I$NATIVE/common")

# Only the compute translation units require AVX; loading utils is baseline-safe.
g++ "${COMMON[@]}" -shared "$NATIVE/utils/utils.cc" \
    -o "$BIN/gkl_utils.dll" -static -static-libgcc -static-libstdc++ \
    -Wl,--no-insert-timestamp -s
g++ "${COMMON[@]}" -mavx -shared "$NATIVE/pairhmm/IntelPairHmm.cc" \
    "$NATIVE/pairhmm/avx_impl.cc" "$NATIVE/pairhmm/pairhmm_common.cc" \
    -o "$BIN/gkl_pairhmm.dll" -static -static-libgcc -static-libstdc++ \
    -Wl,--no-insert-timestamp -s

RES="$SRC_U/.wgse/jar/com/intel/gkl/native"
mkdir -p "$RES" "$PREFIX_U/Library/share/gkl"
for dll in gkl_utils gkl_pairhmm; do
    deps=$(ldd "$BIN/$dll.dll" | grep -viE '/c/windows/' || true)
    if [ -n "$deps" ]; then
        echo "$dll links non-system DLLs: $deps" >&2
        exit 1
    fi
    objdump -p "$BIN/$dll.dll" > "$SRC_U/.wgse/$dll-exports.txt"
    grep -q 'Java_com_intel_gkl_' "$SRC_U/.wgse/$dll-exports.txt"
    cp "$BIN/$dll.dll" "$RES/"
done
cp LICENSE "$SRC_U/.wgse/jar/"
cp README.md "$SRC_U/.wgse/jar/"
"$JAR" --create --date=2024-01-01T00:00:00Z \
    --file "$PREFIX_U/Library/share/gkl/gkl-native-wgse.jar" \
    -C "$SRC_U/.wgse/jar" .
