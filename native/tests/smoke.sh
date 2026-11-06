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
else
    "$binary" capabilities --json > capabilities.json 2> capabilities.log
    if grep -q '"metal_available":true' capabilities.json; then
        # shellcheck disable=SC2086
        "$binary" --metal $gpu_options mem -t 4 reference.fa reads_1.fq reads_2.fq \
            > metal.sam 2> metal.log
        # Only the @PG command line may differ (it records the backend option).
        grep -v '^@PG' cpu.sam > cpu.records
        grep -v '^@PG' metal.sam > metal.records
        cmp cpu.records metal.records
        grep 'gpu_batches=' metal.log
        if grep -q 'gpu_batches=0 ' metal.log; then
            if [ "$mode" = force-gpu ]; then
                echo "force-gpu run dispatched no GPU batches" >&2
                exit 1
            fi
            echo "Routing kept every batch on the CPU; Metal output matches CPU"
        else
            # wgse-bwa also locates seeds on the GPU; mem_chain must use every row.
            if grep -q 'locate_misses=' metal.log; then
                grep -q 'locate_misses=0$' metal.log
                [ "$mode" != force-gpu ] || grep -Eq 'located_rows=[1-9]' metal.log
            fi
            # ...and prepares mate rescue on the GPU; every prepared window must be used.
            if grep -q 'rescue_misses=' metal.log; then
                grep -q 'rescue_misses=0 ' metal.log
                [ "$mode" != force-gpu ] || grep -Eq 'rescue_gpu_pairs=[1-9]' metal.log
            fi
            # ...and extends seeds on the GPU; its results must equal upstream's.
            if grep -q 'extend_hits=' metal.log; then
                grep 'extend_batches=' metal.log
                [ "$mode" != force-gpu ] || grep -Eq 'extend_hits=[1-9]' metal.log
            fi
            echo "Metal alignments match CPU"
        fi
    elif grep -q '"metal_hardware_supported":true' capabilities.json; then
        cat capabilities.log >&2
        echo "Metal hardware is present but the backend failed to initialize" >&2
        exit 1
    else
        echo "Metal unsupported on this machine; CPU alignment verified"
    fi
fi
