# shellcheck shell=bash
# Bit-exactness guard shared by the Arm64 GKL build scripts (source this file).
# The AVX kernels round every multiply and add separately, so the Arm build must not
# contain fused multiply-add instructions. The check fails closed: the disassembly is
# written to a file first (no pipe for an early-exiting reader to SIGPIPE), a failed
# or unrecognised disassembly is an error, and only an explicit "no FMA found" passes.
# A recognised object with no code (data-only translation unit) passes.

# reject_fma OBJDUMP FILE [EXEMPT_ERE]
# Returns 0 if FILE has no FMA outside functions whose demangled header matches
# EXEMPT_ERE, 1 (listing the functions) if it has, and 2 if it cannot be checked.
reject_fma() {
    local objdump=$1 file=$2 exempt=${3:-} dis status=0
    dis="$file.disasm"
    if ! "$objdump" -d -C "$file" > "$dis"; then
        echo "fma guard: $objdump failed on $file" >&2
        rm -f "$dis"
        return 2
    fi
    if ! grep -q 'file format ' "$dis"; then
        echo "fma guard: $objdump did not recognise $file" >&2
        rm -f "$dis"
        return 2
    fi
    awk -v exempt="$exempt" '
        /^[0-9a-f]+ <.*>:$/ { fn = $0 }
        /[[:space:]](fmla|fmls|fmadd|fmsub|fnmadd|fnmsub)[[:space:]]/ &&
            (exempt == "" || fn !~ exempt) { if (!(fn in bad)) { bad[fn] = 1; n++; print fn } }
        END { exit n > 0 }' "$dis" > "$dis.fma" || status=$?
    case $status in
    0) ;;
    1)
        echo "$file contains fused multiply-add instructions in:" >&2
        cat "$dis.fma" >&2
        ;;
    *)
        echo "fma guard: awk failed on $file" >&2
        status=2
        ;;
    esac
    rm -f "$dis" "$dis.fma"
    return "$status"
}

# fma_guard_selftest CXX OBJDUMP DIR [CXXFLAGS...]
# Proves the guard rejects an FMA-bearing object, honours an exemption, and accepts
# a*b+c compiled with the build's flags (which must include -ffp-contract=off).
fma_guard_selftest() {
    local cxx=$1 objdump=$2 dir=$3 status=0
    shift 3
    mkdir -p "$dir"
    printf 'double fused_sample(double a, double b, double c) { return __builtin_fma(a, b, c); }\n' \
        > "$dir/fma_sample.cc"
    printf 'double plain_sample(double a, double b, double c) { return a * b + c; }\n' \
        > "$dir/plain_sample.cc"
    "$cxx" "$@" -O2 -c "$dir/fma_sample.cc" -o "$dir/fma_sample.o"
    "$cxx" "$@" -O2 -c "$dir/plain_sample.cc" -o "$dir/plain_sample.o"
    reject_fma "$objdump" "$dir/fma_sample.o" 2> /dev/null || status=$?
    if [ "$status" != 1 ]; then
        echo "fma guard self-test: FMA sample not rejected (status $status)" >&2
        return 1
    fi
    if ! { reject_fma "$objdump" "$dir/fma_sample.o" 'fused_sample' &&
        reject_fma "$objdump" "$dir/plain_sample.o"; }; then
        echo "fma guard self-test: clean or exempt sample rejected" >&2
        return 1
    fi
    rm -f "$dir"/fma_sample.* "$dir"/plain_sample.*
    echo "fma guard self-test passed"
}
