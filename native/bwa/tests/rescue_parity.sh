#!/bin/sh
# GPU mate rescue (mem_matesw's ksw_align2) must leave the paired SAM unchanged.
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

# A random reference with near-copies of one region, so rescue finds suboptimal
# hits, and pairs in three orientations. Every third mate has a mismatch every
# 12 bp and often an indel, so it has no exact 19-mer seed and is only placed by
# mate rescue.
awk 'BEGIN {
    srand(364)
    split("A C G T", base, " ")
    for (i = 1; i <= 160000; ++i) random = random base[int(rand() * 4) + 1]
    repeat = substr(random, 40001, 3000)
    copy = repeat
    for (i = 50; i < 3000; i += 97) copy = substr(copy, 1, i - 1) base[int(rand() * 4) + 1] substr(copy, i + 1)
    ref = substr(random, 1, 41500) copy substr(random, 44501)
    print ">chr\n" ref > "reference.fa"
    complement["A"] = "T"; complement["C"] = "G"; complement["G"] = "C"; complement["T"] = "A"
    complement["N"] = "N"
    split("151 151 151 101 76 250 300", lengths, " ")
    for (p = 1; p <= 1500; ++p) {
        n = lengths[1 + p % 7]
        insert = 350 + int(rand() * 150)
        start = p % 5 == 0 ? 39000 + int(rand() * 6000) : 1 + int(rand() * (length(ref) - insert))
        left = substr(ref, start, n)
        right = substr(ref, start + insert - n, n)
        if (p % 3 == 0) {
            for (i = 1 + int(rand() * 12); i <= n; i += 12) right = substr(right, 1, i - 1) (substr(right, i, 1) == "A" ? "C" : "A") substr(right, i + 1)
            # Insertions and deletions exercise the striped lazy-F and E updates.
            at = 10 + int(rand() * (n - 20))
            gap = 1 + int(rand() * 4)
            if (p % 6 == 0) {
                inserted = ""
                for (i = 1; i <= gap; ++i) inserted = inserted base[int(rand() * 4) + 1]
                right = substr(right, 1, at) inserted substr(right, at + 1, n - at - gap)
            } else if (p % 9 == 0) {
                right = substr(right, 1, at) substr(right, at + gap + 1) substr(ref, start + insert, gap)
            }
        }
        if (p % 8 == 0) right = substr(right, 1, 30) "N" substr(right, 32)
        if (p % 13 == 0) right = substr(right, 1, 40) substr(right, 44)
        revleft = revright = ""
        for (i = length(left); i >= 1; --i) revleft = revleft complement[substr(left, i, 1)]
        for (i = length(right); i >= 1; --i) revright = revright complement[substr(right, i, 1)]
        orientation = p % 20
        if (orientation < 14) { one = left; two = revright }        # FR
        else if (orientation < 17) { one = revleft; two = right }   # RF (outward)
        else { one = left; two = right }                            # FF
        q1 = q2 = ""
        for (i = 1; i <= length(one); ++i) q1 = q1 "I"
        for (i = 1; i <= length(two); ++i) q2 = q2 "I"
        printf "@pair%d/1\n%s\n+\n%s\n", p, one, q1 > "pairs-R1.fq"
        printf "@pair%d/2\n%s\n+\n%s\n", p, two, q2 > "pairs-R2.fq"
    }
}'
"$aligner" index reference.fa > index.log 2>&1

# Skip (77) only on machines without supported Metal hardware.
"$aligner" capabilities --json > capabilities.json
if ! grep -q '"metal_available":true' capabilities.json; then
    grep -q '"metal_hardware_supported":true' capabilities.json && exit 1
    exit 77
fi

run() {
    name=$1
    shift
    "$aligner" mem "$@" reference.fa pairs-R1.fq pairs-R2.fq > "cpu-$name.sam" 2> "cpu-$name.log"
    "$aligner" --gpu-policy all --gpu-rescue-batch 7 --metal mem "$@" reference.fa \
        pairs-R1.fq pairs-R2.fq > "metal-$name.sam" 2> "metal-$name.log"
    same "cpu-$name.sam" "metal-$name.sam"
    grep '^\[wgse-bwa\] rescue_batches' "metal-$name.log"
}

# Each GPU result is compared with upstream ksw_align2 for the same inputs.
"$aligner" --gpu-policy all --gpu-rescue-batch 7 --gpu-rescue-verify --metal mem -t 4 \
    reference.fa pairs-R1.fq pairs-R2.fq > verify.sam 2> verify.log
"$aligner" mem -t 4 reference.fa pairs-R1.fq pairs-R2.fq > cpu.sam 2> cpu.log
same cpu.sam verify.sam
# Rescue windows cover every orientation BWA infers from the pairs.
for orientation in FF FR RF; do
    grep -q "analyzing insert size distribution for orientation $orientation" cpu.log
done
cat verify.log | grep '^\[wgse-bwa\] rescue'
test "$(metric verify.log rescue_batches)" -ge 3
test "$(metric verify.log rescue_hits)" -gt 1000
test "$(metric verify.log rescue_misses)" -eq 0
test "$(metric verify.log rescue_mismatches)" -eq 0
# Mates over 256 bp stay on upstream ksw_align2.
test "$(metric verify.log rescue_skipped)" -gt 0

for threads in 1 4; do
    run "t$threads" -t "$threads"
    test "$(metric "metal-t$threads.log" rescue_hits)" -gt 1000
done
# Small chunks: Metal maps each chunk while the previous one pairs.
run chunks -t 4 -K 20000
test "$(metric metal-chunks.log rescue_hits)" -gt 0
test "$(metric metal-chunks.log overlapped_chunks)" -gt 0
# macOS aborts command buffers that delay the display (ImpactingInteractivity),
# for example while another process uses the GPU; that work is committed again.
# Report every second piece of seeding, locate, rescue and extension work as
# interrupted, so its kernels run twice: the results must not change.
"$aligner" --gpu-inject-interruptions 2 --gpu-policy all --gpu-rescue-batch 7 \
    --gpu-rescue-verify --gpu-extend-verify --metal mem -t 4 -K 20000 reference.fa \
    pairs-R1.fq pairs-R2.fq > interrupted.sam 2> interrupted.log
same cpu-chunks.sam interrupted.sam
test "$(grep -c 'CPU fallback' interrupted.log)" -eq 0
test "$(metric interrupted.log interruptions)" -gt 0
test "$(metric interrupted.log rescue_hits)" -gt 0
test "$(metric interrupted.log rescue_mismatches)" -eq 0
test "$(metric interrupted.log extend_hits)" -gt 0
test "$(metric interrupted.log extend_mismatches)" -eq 0
# Other gap penalties and clipping; a manual insert size distribution.
run gaps -t 4 -O 4,8 -E 2,1 -L 3,7 -U 9
test "$(metric metal-gaps.log rescue_hits)" -gt 0
run insert -t 4 -I 420,40
test "$(metric metal-insert.log rescue_hits)" -gt 0
# -P skips pairing but still rescues mates.
run nopair -t 4 -P
test "$(metric metal-nopair.log rescue_hits)" -gt 0
# A larger match score makes mem_matesw use 16-bit ksw_i16, which stays on the CPU.
run wide -t 4 -A 4 -B 8
test "$(metric metal-wide.log rescue_hits)" -eq 0
test "$(metric metal-wide.log rescue_skipped)" -gt 0
# -S disables rescue entirely.
run norescue -t 4 -S
test "$(metric metal-norescue.log rescue_batches)" -eq 0

# A large mismatch penalty saturates ksw_u8 on exact 151 bp mates (which -k 160
# keeps from seeding); upstream then runs an empty reverse pass, so the GPU
# declines those windows and mem_matesw() computes them as upstream does.
awk 'BEGIN {
    srand(7)
    while ((getline line < "reference.fa") > 0) if (line !~ /^>/) ref = ref line
    complement["A"] = "T"; complement["C"] = "G"; complement["G"] = "C"; complement["T"] = "A"
    for (p = 1; p <= 200; ++p) {
        start = 1 + int(rand() * (length(ref) - 500))
        right = substr(ref, start + 380 + int(rand() * 40) - 151, 151)
        two = ""
        for (i = 151; i >= 1; --i) two = two complement[substr(right, i, 1)]
        q1 = q2 = ""
        for (i = 1; i <= 200; ++i) q1 = q1 "I"
        for (i = 1; i <= 151; ++i) q2 = q2 "I"
        printf "@sat%d/1\n%s\n+\n%s\n", p, substr(ref, start, 200), q1 > "sat-R1.fq"
        printf "@sat%d/2\n%s\n+\n%s\n", p, two, q2 > "sat-R2.fq"
    }
}'
"$aligner" mem -t 4 -k 160 -B 120 -I 400,40 reference.fa sat-R1.fq sat-R2.fq \
    > cpu-saturate.sam 2> cpu-saturate.log
"$aligner" --gpu-policy all --metal mem -t 4 -k 160 -B 120 -I 400,40 reference.fa \
    sat-R1.fq sat-R2.fq > metal-saturate.sam 2> metal-saturate.log
same cpu-saturate.sam metal-saturate.sam
test "$(metric metal-saturate.log rescue_declined)" -eq 200
test "$(metric metal-saturate.log rescue_misses)" -eq 0

# A rescue batch that fails after the first one (while the next is packed) is
# finished on the CPU, as is the rest. Explicit --metal still reports it.
status=0
"$aligner" --gpu-inject-rescue-failure --gpu-policy all --gpu-rescue-batch 7 --metal mem -t 4 reference.fa \
    pairs-R1.fq pairs-R2.fq > failed-metal.sam 2> failed-metal.log || status=$?
test "$status" -eq 1
grep -q 'CPU fallback: injected GPU rescue failure' failed-metal.log
test "$(metric failed-metal.log rescue_batches)" -ge 1
grep -q 'Metal execution failed (alignments were completed on the CPU)' failed-metal.log
same cpu.sam failed-metal.sam
"$aligner" --gpu-inject-rescue-failure --gpu-policy all --gpu-rescue-batch 7 --backend auto mem -t 4 \
    reference.fa pairs-R1.fq pairs-R2.fq > failed-auto.sam 2> failed-auto.log
grep -q 'CPU fallback: injected GPU rescue failure' failed-auto.log
same cpu.sam failed-auto.sam
# The same while the next chunk is being mapped on the GPU.
status=0
"$aligner" --gpu-inject-rescue-failure --gpu-policy all --gpu-rescue-batch 7 --metal mem -t 4 \
    -K 20000 reference.fa pairs-R1.fq pairs-R2.fq > failed-chunks.sam 2> failed-chunks.log \
    || status=$?
test "$status" -eq 1
grep -q 'CPU fallback: injected GPU rescue failure' failed-chunks.log
same cpu-chunks.sam failed-chunks.sam

# CPU threads align the rescue windows of a pair they take in one interleaved
# batch (ksw_oracle.c.in). After a GPU rescue failure they take every remaining
# pair; verification compares each batched result with upstream ksw_align2.
cpu_batch() {
    name=$1
    shift
    "$aligner" --gpu-inject-rescue-failure --gpu-rescue-verify --gpu-policy all \
        --gpu-rescue-batch 7 --backend auto mem "$@" reference.fa pairs-R1.fq pairs-R2.fq \
        > "batch-$name.sam" 2> "batch-$name.log"
    same "cpu-$name.sam" "batch-$name.sam"
    grep '^\[wgse-bwa\] rescue_batches' "batch-$name.log"
    test "$(metric "batch-$name.log" rescue_cpu_hits)" -gt 100
    test "$(metric "batch-$name.log" rescue_mismatches)" -eq 0
}
cpu_batch t4 -t 4
cpu_batch gaps -t 4 -O 4,8 -E 2,1 -L 3,7 -U 9
# 16-bit windows in the batch run upstream ksw_align2.
cpu_batch wide -t 4 -A 4 -B 8
