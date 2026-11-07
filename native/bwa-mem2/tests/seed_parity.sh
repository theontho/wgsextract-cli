#!/bin/sh
# GPU SMEM collection (mem_collect_smem's three passes) must leave the SAM unchanged.
set -eu
aligner=$1
work=$2
mkdir -p "$work"
cd "$work"

same() {
    grep -v '^@PG' "$1" > "$1.records"
    grep -v '^@PG' "$2" > "$2.records"
    cmp "$1.records" "$2.records"
}
metric() {
    sed -n "s/.* $2=\([0-9]*\).*/\1/p" "$1"
}

# A random reference with a family of diverged repeats (many SMEMs and
# re-seeding) and reads of varied lengths with errors, Ns and IUPAC codes.
# Single-end reads include random sequence and reads too long for the GPU.
awk 'BEGIN {
    srand(1364)
    split("A C G T", base, " ")
    for (i = 1; i <= 200000; ++i) random = random base[int(rand() * 4) + 1]
    element = substr(random, 100001, 1500)
    ref = substr(random, 1, 60000)
    for (c = 0; c < 24; ++c) {
        copy = element
        for (i = 1 + int(rand() * 40); i < 1500; i += 20 + int(rand() * 60))
            copy = substr(copy, 1, i - 1) base[int(rand() * 4) + 1] substr(copy, i + 1)
        ref = ref copy substr(random, 60001 + c * 1000, 1000)
    }
    ref = ref substr(random, 120001)
    print ">chr\n" ref > "reference.fa"
    complement["A"] = "T"; complement["C"] = "G"; complement["G"] = "C"; complement["T"] = "A"
    split("151 151 151 101 76 250 36 300", lengths, " ")
    for (p = 1; p <= 6000; ++p) {
        n = lengths[1 + p % 8]
        insert = 350 + int(rand() * 150)
        start = p % 3 == 0 ? 60000 + int(rand() * 55000) : 1 + int(rand() * (length(ref) - insert))
        for (mate = 1; mate <= 2; ++mate) {
            read = substr(ref, mate == 1 ? start : start + insert - n, n)
            for (i = 1 + int(rand() * 30); i <= n; i += 10 + int(rand() * 40))
                read = substr(read, 1, i - 1) base[int(rand() * 4) + 1] substr(read, i + 1)
            if (p % 11 == 0) read = substr(read, 1, 20) "N" substr(read, 22)
            if (p % 17 == 0) read = substr(read, 1, 30) "R" substr(read, 32)
            if (p % 19 == 0) read = substr(read, 1, n - 1) "N"
            if (mate == 2) {
                reverse = ""
                for (i = n; i >= 1; --i) {
                    c = substr(read, i, 1)
                    reverse = reverse (c in complement ? complement[c] : c)
                }
                read = reverse
            }
            q = ""
            for (i = 1; i <= length(read); ++i) q = q "I"
            printf "@pair%d/%d\n%s\n+\n%s\n", p, mate, read, q > ("pairs-R" mate ".fq")
            if (mate == 1) printf "@single%d\n%s\n+\n%s\n", p, read, q > "single.fq"
        }
        if (p % 50 == 0) {
            read = ""
            for (i = 1; i <= 120; ++i) read = read base[int(rand() * 4) + 1]
            printf "@random%d\n%s\n+\n%s\n", p, read, substr(q, 1, 120) > "single.fq"
        }
    }
    for (p = 1; p <= 3; ++p) {
        read = substr(ref, 1000 + p * 7000, 600)
        q = ""
        for (i = 1; i <= 600; ++i) q = q "I"
        printf "@long%d\n%s\n+\n%s\n", p, read, q > "long.fq"
    }
}'
cat single.fq long.fq > mixed.fq
"$aligner" index reference.fa > index.log 2>&1

# Skip (77) only on machines without supported Metal hardware.
"$aligner" capabilities --json > capabilities.json
if ! grep -q '"metal_available":true' capabilities.json; then
    grep -q '"metal_hardware_supported":true' capabilities.json && exit 1
    exit 77
fi

# One 2048-read unit per GPU batch, so chunks span several batches.
gpu="--metal --gpu-policy all --gpu-seed-batch 2048"
run() {
    name=$1
    reads=$2
    shift 2
    "$aligner" mem "$@" reference.fa $reads > "cpu-$name.sam" 2> "cpu-$name.log"
    # shellcheck disable=SC2086
    "$aligner" $gpu --gpu-seed-verify mem "$@" reference.fa $reads \
        > "metal-$name.sam" 2> "metal-$name.log"
    same "cpu-$name.sam" "metal-$name.sam"
    grep '^\[wgse-bwa-mem2\] seed_' "metal-$name.log"
    test "$(metric "metal-$name.log" seed_hits)" -gt 0
    test "$(metric "metal-$name.log" seed_mismatches)" -eq 0
}

# Each unit's GPU SMEMs are compared with upstream mem_collect_smem's.
run paired "pairs-R1.fq pairs-R2.fq" -t 4
test "$(metric metal-paired.log seed_batches)" -ge 5
test "$(metric metal-paired.log seed_misses)" -eq 0
run single single.fq -t 1
# Seeding options, a third pass disabled or with a negative int32 limit.
run options "pairs-R1.fq pairs-R2.fq" -t 4 -k 15 -r 1.0 -s 20 -y 0
run wide "pairs-R1.fq pairs-R2.fq" -t 4 -k 25 -r 3 -s 3 -y 3000000000
run third "pairs-R1.fq pairs-R2.fq" -t 4 -y 200
# Small chunks; the GPU starts seeding each one when it is read.
run chunks "pairs-R1.fq pairs-R2.fq" -t 4 -K 100000
test "$(metric metal-chunks.log seed_preseeded_chunks)" -gt 0
test "$(metric metal-chunks.log seed_misses)" -eq 0
# Reads with more SMEMs than the GPU keeps are collected on the CPU.
gpu="$gpu --gpu-seed-capacity 4"
run capacity "pairs-R1.fq pairs-R2.fq" -t 4
test "$(metric metal-capacity.log seed_cpu_reads)" -gt 100
test "$(metric metal-capacity.log seed_misses)" -eq 0
# A read too long for the GPU leaves its unit to upstream.
gpu="--metal --gpu-policy all --gpu-seed-batch 2048"
run long mixed.fq -t 4
test "$(metric metal-long.log seed_misses)" -ge 1
# Without verification, with interruptions: every second GPU dispatch reruns.
"$aligner" $gpu --gpu-inject-interruptions 2 mem -t 4 reference.fa pairs-R1.fq pairs-R2.fq \
    > interrupted.sam 2> interrupted.log
same cpu-paired.sam interrupted.sam
test "$(metric interrupted.log interruptions)" -gt 0
test "$(metric interrupted.log seed_hits)" -gt 0
"$aligner" $gpu --gpu-seed off mem -t 4 reference.fa pairs-R1.fq pairs-R2.fq \
    > off.sam 2> off.log
same cpu-paired.sam off.sam
test "$(metric off.log seed_batches)" -eq 0

# A seeding batch that fails after the first one is finished on the CPU, as
# is the rest; explicit --metal still reports it.
status=0
"$aligner" $gpu --gpu-inject-seed-failure mem -t 4 reference.fa pairs-R1.fq pairs-R2.fq \
    > failed-metal.sam 2> failed-metal.log || status=$?
test "$status" -eq 1
grep -q 'CPU fallback: injected GPU seeding failure' failed-metal.log
grep -q 'Metal execution failed (alignments were completed on the CPU)' failed-metal.log
test "$(metric failed-metal.log seed_hits)" -gt 0
same cpu-paired.sam failed-metal.sam
"$aligner" --backend auto --gpu-policy all --gpu-seed-batch 2048 --gpu-inject-seed-failure \
    mem -t 4 reference.fa pairs-R1.fq pairs-R2.fq > failed-auto.sam 2> failed-auto.log
grep -q 'CPU fallback: injected GPU seeding failure' failed-auto.log
same cpu-paired.sam failed-auto.sam
# The same with two chunks of several batches, seeded ahead.
"$aligner" mem -t 4 -K 1000000 reference.fa pairs-R1.fq pairs-R2.fq > cpu-two.sam 2> cpu-two.log
status=0
# shellcheck disable=SC2086
"$aligner" $gpu --gpu-inject-seed-failure mem -t 4 -K 1000000 reference.fa pairs-R1.fq \
    pairs-R2.fq > failed-chunks.sam 2> failed-chunks.log || status=$?
test "$status" -eq 1
grep -q 'CPU fallback: injected GPU seeding failure' failed-chunks.log
test "$(metric failed-chunks.log seed_preseeded_chunks)" -ge 1
same cpu-two.sam failed-chunks.sam
