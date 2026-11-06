#!/bin/sh
# GPU seed extension (mem_chain2aln's ksw_extend2) must leave the SAM unchanged.
set -eu
aligner=$1
work=$2
mkdir -p "$work"
cd "$work"

# The @PG command line differs by the backend option; every other line must match.
same() {
    grep -v '^@PG' "$1" > "$1.records"
    grep -v '^@PG' "$2" > "$2.records"
    cmp "$1.records" "$2.records"
}
metric() {
    sed -n "s/.* $2=\([0-9]*\).*/\1/p" "$1"
}

# A random reference with diverged copies of one region (several chains per
# read), and pairs whose mates carry mismatches, indels up to 30 bp (band
# retries with a narrow -w), N bases, and foreign tails (Z-drop and clipping).
# Mates of 300 bp have sides over 256 bp, which stay on upstream ksw_extend2.
awk 'BEGIN {
    srand(2364)
    split("A C G T", base, " ")
    for (i = 1; i <= 200000; ++i) random = random base[int(rand() * 4) + 1]
    copy = substr(random, 60001, 4000)
    for (i = 30; i < 4000; i += 61) copy = substr(copy, 1, i - 1) base[int(rand() * 4) + 1] substr(copy, i + 1)
    ref = substr(random, 1, 120000) copy substr(random, 124001)
    print ">chr\n" ref > "reference.fa"
    complement["A"] = "T"; complement["C"] = "G"; complement["G"] = "C"; complement["T"] = "A"
    complement["N"] = "N"
    split("151 151 151 101 76 250 300", lengths, " ")
    for (p = 1; p <= 2000; ++p) {
        insert = 380 + int(rand() * 120)
        start = p % 4 == 0 ? 59000 + int(rand() * 5000) : 1 + int(rand() * (length(ref) - insert - 400))
        for (m = 1; m <= 2; ++m) {
            n = lengths[1 + (p + m) % 7]
            read = m == 1 ? substr(ref, start, n + 40) : substr(ref, start + insert - n, n + 40)
            for (i = 1 + int(rand() * 40); i <= n; i += 20 + int(rand() * 40))
                read = substr(read, 1, i - 1) (substr(read, i, 1) == "A" ? "C" : "A") substr(read, i + 1)
            kind = (p * 2 + m) % 11
            at = 20 + int(rand() * (n - 40))
            gap = kind < 3 ? 1 + int(rand() * 6) : 10 + int(rand() * 21)
            if (kind == 0 || kind == 5) {
                inserted = ""
                for (i = 1; i <= gap; ++i) inserted = inserted base[int(rand() * 4) + 1]
                read = substr(read, 1, at) inserted substr(read, at + 1)
            } else if (kind == 1 || kind == 6) {
                read = substr(read, 1, at) substr(read, at + gap + 1)
            } else if (kind == 7) {
                read = substr(read, 1, n - 50) substr(ref, 1 + int(rand() * 100000), 50)
            } else if (kind == 8) {
                read = substr(read, 1, at) "N" substr(read, at + 2)
            }
            read = substr(read, 1, n)
            if (m == 2) {
                reverse = ""
                for (i = n; i >= 1; --i) reverse = reverse complement[substr(read, i, 1)]
                read = reverse
            }
            quality = ""
            for (i = 1; i <= n; ++i) quality = quality "I"
            printf "@pair%d/%d\n%s\n+\n%s\n", p, m, read, quality > ("pairs-R" m ".fq")
        }
    }
}'
"$aligner" index reference.fa > index.log 2>&1

# Skip (77) only on machines without supported Metal hardware.
"$aligner" capabilities --json > capabilities.json
if ! grep -q '"metal_available":true' capabilities.json; then
    grep -q '"metal_hardware_supported":true' capabilities.json && exit 1
    exit 77
fi

"$aligner" mem -t 4 reference.fa pairs-R1.fq pairs-R2.fq > cpu.sam 2> cpu.log

# Each GPU result is compared with upstream ksw_extend2 for the same inputs, over
# many small batches. Calls for seeds other than each chain's first are not
# staged and miss.
"$aligner" --gpu-policy all --gpu-extend-pairs 16 --gpu-extend-batches 64 --gpu-extend-verify \
    --metal mem -t 4 \
    reference.fa pairs-R1.fq pairs-R2.fq > verify.sam 2> verify.log
same cpu.sam verify.sam
grep '^\[wgse-bwa\] extend_' verify.log
test "$(metric verify.log extend_batches)" -ge 20
test "$(metric verify.log extend_hits)" -gt 3000
test "$(metric verify.log extend_hits)" -gt "$(metric verify.log extend_misses)"
test "$(metric verify.log extend_mismatches)" -eq 0
test "$(metric verify.log extend_skipped)" -gt 0

run() {
    name=$1
    shift
    "$aligner" mem "$@" reference.fa pairs-R1.fq pairs-R2.fq > "cpu-$name.sam" 2> "cpu-$name.log"
    "$aligner" --gpu-policy all --gpu-extend-pairs 64 --gpu-extend-batches 16 \
        --gpu-extend-verify --metal mem "$@" \
        reference.fa pairs-R1.fq pairs-R2.fq > "metal-$name.sam" 2> "metal-$name.log"
    same "cpu-$name.sam" "metal-$name.sam"
    grep '^\[wgse-bwa\] extend_' "metal-$name.log"
    test "$(metric "metal-$name.log" extend_hits)" -gt 0
    test "$(metric "metal-$name.log" extend_mismatches)" -eq 0
}
for threads in 1 9; do
    run "t$threads" -t "$threads"
done
# A narrow band (retries with a doubled band), other scores, gap penalties,
# clipping penalties and Z-drop.
run narrow -t 4 -w 8 -d 30
test "$(metric metal-narrow.log extend_retry_hits)" -gt 0
run scores -t 4 -A 2 -B 5 -O 7,9 -E 2,1 -L 3,8 -d 60
# High scores and insertion extension: long sides would overflow the kernel's
# 16-bit F scan, so they stay on upstream ksw_extend2.
run wide -t 4 -A 100 -B 50 -E 50
test "$(metric metal-wide.log extend_skipped)" -gt 100
# Single-end reads.
"$aligner" mem -t 4 reference.fa pairs-R1.fq > cpu-single.sam 2> cpu-single.log
"$aligner" --gpu-extend-verify --metal mem -t 4 reference.fa pairs-R1.fq \
    > metal-single.sam 2> metal-single.log
same cpu-single.sam metal-single.sam
test "$(metric metal-single.log extend_hits)" -gt 0
test "$(metric metal-single.log extend_mismatches)" -eq 0
# With one batch, pairs that find no free batch are extended on their CPU thread.
"$aligner" --gpu-policy all --gpu-extend-pairs 4 --gpu-extend-batches 1 --metal mem -t 9 \
    reference.fa pairs-R1.fq pairs-R2.fq > one.sam 2> one.log
same cpu.sam one.sam
test "$(metric one.log extend_hits)" -gt 0
# --gpu-extend off leaves extension on the CPU.
"$aligner" --gpu-extend off --metal mem -t 4 reference.fa pairs-R1.fq pairs-R2.fq \
    > off.sam 2> off.log
same cpu.sam off.sam
test "$(metric off.log extend_batches)" -eq 0

# A failed extension batch is finished on the CPU. Explicit --metal still reports it.
status=0
"$aligner" --gpu-inject-extend-failure --metal mem -t 4 reference.fa pairs-R1.fq pairs-R2.fq \
    > failed-metal.sam 2> failed-metal.log || status=$?
test "$status" -eq 1
grep -q 'CPU fallback: injected GPU extension failure' failed-metal.log
grep -q 'Metal execution failed (alignments were completed on the CPU)' failed-metal.log
test "$(metric failed-metal.log extend_hits)" -eq 0
same cpu.sam failed-metal.sam
"$aligner" --gpu-inject-extend-failure --backend auto mem -t 4 reference.fa pairs-R1.fq \
    pairs-R2.fq > failed-auto.sam 2> failed-auto.log
grep -q 'CPU fallback: injected GPU extension failure' failed-auto.log
same cpu.sam failed-auto.sam
