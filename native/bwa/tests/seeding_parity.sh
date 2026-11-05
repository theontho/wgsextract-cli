#!/bin/sh
# Exercise all three seeding rounds, divergent reads and each fallback path.
set -eu
aligner=$1
benchmark=$2
work=$3
mkdir -p "$work"
cd "$work"

# The @PG command line differs by the backend option; every other line must match.
same() {
    grep -v '^@PG' "$1" > "$1.records"
    grep -v '^@PG' "$2" > "$2.records"
    cmp "$1.records" "$2.records"
}

awk 'BEGIN {
    srand(364)
    split("A C G T", base, " ")
    for (i = 1; i <= 120000; ++i) random = random base[int(rand() * 4) + 1]
    for (i = 1; i <= 24000; ++i) tandem = tandem "ACGT"
    for (i = 1; i <= 96000; ++i) homo = homo "A"
    print ">random\n" random "\n>tandem\n" tandem "\n>homopolymer\n" homo > "reference.fa"
    split("1 18 19 20 28 29 30 100 150 250 511 512 513 900", lengths, " ")
    for (r = 1; r <= 2048; ++r) {
        n = lengths[1 + (r % 14)]
        start = 1 + int(rand() * 110000)
        seq = substr(random, start, n)
        if (r % 7 == 0) seq = substr(tandem, 1 + r % 4, n)
        if (r % 11 == 0) seq = substr(homo, 1, n)
        if (r % 3 == 0) {
            for (i = 15; i < n; i += 23)
                seq = substr(seq, 1, i - 1) base[int(rand() * 4) + 1] substr(seq, i + 1)
        }
        if (r % 5 == 0) {
            for (i = 1; i <= n; i += 19) seq = substr(seq, 1, i - 1) "N" substr(seq, i + 1)
        }
        qual = ""
        for (i = 1; i <= n; ++i) qual = qual "I"
        printf "@read%d\n%s\n+\n%s\n", r, seq, qual > "reads.fq"
    }
}'
"$aligner" index reference.fa > index.log 2>&1
# Preserve exit 77 when the test runner has no Metal device.
"$benchmark" reference.fa reads.fq --reads 2048 --batch 257 --verify-only > parity.log 2>&1
cat parity.log
grep -Eq 'length=[1-9][0-9]*' parity.log
"$benchmark" reference.fa reads.fq --reads 2048 --batch 257 --verify-only \
    --scratch-limit 1 > scratch-fallback.log 2>&1
cat scratch-fallback.log
grep -Eq 'scratch=[1-9][0-9]*' scratch-fallback.log
"$benchmark" reference.fa reads.fq --reads 2048 --batch 257 --verify-only \
    --output-limit 1 > output-fallback.log 2>&1
cat output-fallback.log
grep -Eq 'output=[1-9][0-9]*' output-fallback.log
# GPU suffix-array locate, in dispatches smaller than one batch's rows.
"$benchmark" reference.fa reads.fq --reads 2048 --batch 257 --verify-only --locate \
    --locate-batch 4093 > locate.log 2>&1
cat locate.log
grep -Eq 'locate_parity=exact rows=[1-9][0-9]*' locate.log
grep -q 'locate_all=exact' locate.log

# Tiny indexes exercise sentinel/block boundaries and absent DNA symbols.
awk 'BEGIN {
    for (i = 1; i <= 127; ++i) seq = seq "A"
    print ">tiny\n" seq > "tiny.fa"
    split("A C G T N", base, " ")
    for (b = 1; b <= 5; ++b) {
        for (n = 18; n <= 151; ++n) {
            read = ""; quality = ""
            for (i = 1; i <= n; ++i) { read = read base[b]; quality = quality "I" }
            printf "@tiny%d_%d\n%s\n+\n%s\n", b, n, read, quality > "tiny.fq"
        }
    }
}'
"$aligner" index tiny.fa > tiny-index.log 2>&1
"$benchmark" tiny.fa tiny.fq --reads 1000 --batch 257 --verify-only > tiny-parity.log 2>&1
cat tiny-parity.log
"$benchmark" tiny.fa tiny.fq --reads 1000 --batch 257 --verify-only --locate \
    > tiny-locate.log 2>&1
cat tiny-locate.log
grep -q 'locate_all=exact' tiny-locate.log

# Regression (PR #444 review): a chunk whose GPU reads overflow, followed by a
# chunk with no GPU-eligible reads (all over 512 bp), must not reuse the first
# chunk's failed-read indices.
awk 'BEGIN {
    for (r = 1; r <= 3; ++r) {
        read = ""; quality = ""
        for (i = 1; i <= 900; ++i) { read = read "A"; quality = quality "I" }
        printf "@long%d\n%s\n+\n%s\n", r, read, quality
    }
}' | cat tiny.fq - > chunks.fq
tiny_bases=$(awk 'NR % 4 == 2 { n += length($0) } END { print n }' tiny.fq)
"$aligner" mem -t 1 -K "$tiny_bases" tiny.fa chunks.fq > cpu-chunks.sam 2> cpu-chunks.log
"$aligner" --gpu-policy all --metal mem -t 1 -K "$tiny_bases" tiny.fa chunks.fq \
    > metal-chunks.sam 2> metal-chunks.log
same cpu-chunks.sam metal-chunks.sam
test "$(grep -c 'Processed' metal-chunks.log)" -eq 2
# More fallbacks than the three long reads: some first-chunk GPU reads overflowed.
fallbacks=$(sed -n 's/.*fallback_reads=\([0-9]*\).*/\1/p' metal-chunks.log)
test "$fallbacks" -gt 3

"$benchmark" reference.fa reads.fq --reads 2048 --batch 257 --verify-only \
    --concurrent --scratch-limit 1 --output-limit 1 > concurrent-parity.log 2>&1
cat concurrent-parity.log
grep -q 'concurrent_parity=exact' concurrent-parity.log

# Regression: cached GPU intervals must preserve full SAM output, including
# overlength/repetitive fallbacks, single-worker execution and partial chunks.
"$aligner" mem -t 1 -K 10000 reference.fa reads.fq > cpu-edges.sam 2> cpu-edges.log
"$aligner" --gpu-policy all --metal mem -t 1 -K 10000 reference.fa reads.fq \
    > metal-edges.sam 2> metal-edges.log
same cpu-edges.sam metal-edges.sam
grep -Eq 'gpu_batches=[1-9][0-9]* .*fallback_reads=[1-9][0-9]*' metal-edges.log
# Every GPU-located row is consumed by mem_chain in order.
grep -Eq 'located_rows=[1-9][0-9]* locate_misses=0$' metal-edges.log
"$aligner" --gpu-policy all --gpu-locate off --metal mem -t 1 -K 10000 reference.fa reads.fq \
    > seeds-only.sam 2> seeds-only.log
same cpu-edges.sam seeds-only.sam
grep -Eq 'gpu_batches=[1-9][0-9]* .*located_rows=0 ' seeds-only.log

# -c changes how many rows mem_chain visits and how it steps through repeats.
for occ in 1 7 50; do
    "$aligner" mem -t 2 -c "$occ" -K 20000 reference.fa reads.fq > "cpu-c$occ.sam" \
        2> "cpu-c$occ.log"
    "$aligner" --gpu-policy all --gpu-batch 300 --metal mem -t 2 -c "$occ" -K 20000 \
        reference.fa reads.fq > "metal-c$occ.sam" 2> "metal-c$occ.log"
    same "cpu-c$occ.sam" "metal-c$occ.sam"
    grep -Eq 'located_rows=[1-9][0-9]* locate_misses=0$' "metal-c$occ.log"
done

"$aligner" mem -t 3 -K 20000 -k 11 -r 1.1 -s 5 -y 0 reference.fa reads.fq \
    > cpu-options.sam 2> cpu-options.log
"$aligner" --metal mem -t 3 -K 20000 -k 11 -r 1.1 -s 5 -y 0 reference.fa reads.fq \
    > metal-options.sam 2> metal-options.log
same cpu-options.sam metal-options.sam
# How the hybrid policy splits pairs depends on timing; only parity is fixed.
grep -Eq 'gpu_batches=[0-9]+ ' metal-options.log

# A 64-bit -y value must use upstream collection, not silently narrow to 32 bits.
"$aligner" mem -t 3 -y 2147483648 tiny.fa tiny.fq > cpu-wide.sam 2> cpu-wide.log
"$aligner" --metal mem -t 3 -y 2147483648 tiny.fa tiny.fq \
    > metal-wide.sam 2> metal-wide.log
same cpu-wide.sam metal-wide.sam
grep -q 'gpu_batches=0 ' metal-wide.log

# A failed GPU batch is finished on the CPU. Explicit --metal still reports it.
status=0
"$aligner" --gpu-inject-failure --metal mem -t 3 -K 20000 reference.fa reads.fq \
    > failed-metal.sam 2> failed-metal.log || status=$?
test "$status" -eq 1
grep -q 'Metal execution failed (alignments were completed on the CPU)' failed-metal.log
"$aligner" mem -t 3 -K 20000 reference.fa reads.fq > cpu-default.sam 2> cpu-default.log
same cpu-default.sam failed-metal.sam
"$aligner" --gpu-inject-failure --backend auto mem -t 3 -K 20000 reference.fa reads.fq \
    > failed-auto.sam 2> failed-auto.log
grep -q 'CPU fallback: injected GPU failure' failed-auto.log
same cpu-default.sam failed-auto.sam

# A failed locate dispatch is handled like a failed seeding batch.
status=0
"$aligner" --gpu-inject-locate-failure --gpu-policy all --metal mem -t 3 -K 20000 \
    reference.fa reads.fq > failed-locate.sam 2> failed-locate.log || status=$?
test "$status" -eq 1
grep -q 'CPU fallback: injected GPU locate failure' failed-locate.log
same cpu-default.sam failed-locate.sam
"$aligner" --gpu-inject-locate-failure --gpu-policy all --backend auto mem -t 3 -K 20000 \
    reference.fa reads.fq > failed-locate-auto.sam 2> failed-locate-auto.log
grep -q 'CPU fallback: injected GPU locate failure' failed-locate-auto.log
same cpu-default.sam failed-locate-auto.sam
