#!/bin/sh
# Index and align a deterministic synthetic genome; require accurate CPU
# mappings and, for metal modes when Metal is usable, byte-identical alignments.
# Usage: smoke.sh <aligner> <work-dir> [metal|force-gpu]
set -eu
binary=$1
work=$2
mode=${3:-cpu}
gpu_options=""
[ "$mode" = force-gpu ] && gpu_options="--gpu-policy all"

rm -rf "$work"
mkdir -p "$work"
cd "$work"

awk 'BEGIN {
    srand(364)
    split("A C G T", base, " ")
    complement["A"] = "T"; complement["C"] = "G"
    complement["G"] = "C"; complement["T"] = "A"
    contigs = 2; length_ = 120000; pairs = 3000; read = 150
    for (c = 1; c <= contigs; c++) {
        sequence[c] = ""
        for (i = 0; i < length_; i++) sequence[c] = sequence[c] base[int(rand() * 4) + 1]
        print ">chr" c > "reference.fa"
        for (i = 1; i <= length_; i += 60) print substr(sequence[c], i, 60) > "reference.fa"
    }
    quality = ""
    for (i = 0; i < read; i++) quality = quality "I"
    for (p = 1; p <= pairs; p++) {
        c = int(rand() * contigs) + 1
        insert = 300 + int(rand() * 200)
        start = int(rand() * (length_ - insert)) + 1
        left = mutate(substr(sequence[c], start, read))
        right = mutate(substr(sequence[c], start + insert - read, read))
        reverse = ""
        for (i = read; i >= 1; i--) reverse = reverse complement[substr(right, i, 1)]
        name = "p" p "_chr" c "_" start
        printf "@%s/1\n%s\n+\n%s\n", name, left, quality > "reads_1.fq"
        printf "@%s/2\n%s\n+\n%s\n", name, reverse, quality > "reads_2.fq"
    }
}
function mutate(read_sequence,    i, position) {
    for (i = 0; i < 3; i++) {
        position = int(rand() * length(read_sequence)) + 1
        read_sequence = substr(read_sequence, 1, position - 1) base[int(rand() * 4) + 1] \
            substr(read_sequence, position + 1)
    }
    return read_sequence
}'

"$binary" index reference.fa > index.log 2>&1
"$binary" mem -t 4 reference.fa reads_1.fq reads_2.fq > cpu.sam 2> cpu.log
grep -q '^@PG	ID:bwa[-a-z0-9]*	PN:bwa[-a-z0-9]*	VN:[^	]*-wgse-' cpu.sam
awk -F '\t' '
    /^@/ { next }
    !bit($2, 256) && !bit($2, 2048) && bit($2, 64) {
        total++
        split($1, truth, "_")
        if ($3 == truth[2] && $4 >= truth[3] - 10 && $4 <= truth[3] + 10) correct++
    }
    END {
        printf "first-in-pair reads=%d correctly placed=%d\n", total, correct
        exit !(total == 3000 && correct >= 2970)
    }
    function bit(flags, value) { return int(flags / value) % 2 }' cpu.sam

if [ "$mode" = cpu ]; then
    echo "CPU alignment verified"
elif "$binary" capabilities --json 2>/dev/null | grep -q '"metal_available":true'; then
    # shellcheck disable=SC2086
    "$binary" --metal $gpu_options mem -t 4 reference.fa reads_1.fq reads_2.fq \
        > metal.sam 2> metal.log
    grep -v '^@' cpu.sam > cpu.records
    grep -v '^@' metal.sam > metal.records
    cmp cpu.records metal.records
    grep 'gpu_batches=' metal.log
    echo "Metal alignments match CPU"
else
    echo "Metal unavailable; CPU alignment verified"
fi
