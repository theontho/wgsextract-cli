#!/bin/bash
# Build the benchmark datasets that bench.sh fetches, from hs38, its BWA and
# BWA-MEM2 indexes, and the first 5M HG002 pairs, and stage them for upload as
# release assets. Run once on a Mac with about 20 GB of free memory (indexing
# chr1-3 for BWA-MEM2 peaks near 16 GB); it is not part of CI.
#
# Usage: make_data.sh --hs38 DIR --reads R1.fastq.gz R2.fastq.gz
#                     --subset R1.fastq.gz R2.fastq.gz --bin DIR --out DIR
#   --hs38 DIR     hs38.fa with its BWA index and hs38.fa.{bwt.2bit.64,0123}
#   --reads        the 5M-pair reads (tier full-5m)
#   --subset       their first 200k pairs (tier full)
#   --bin DIR      wgse-bwa and wgse-bwa-mem2 (CPU backends, identical to upstream)
#   --out DIR      writes data/ (the layout bench.sh expects), assets/ (files
#                  to upload), data.tsv and expected.tsv
set -euo pipefail

usage() { sed -n '2,14p' "$0" >&2; exit 2; }
hs38="" reads1="" reads2="" subset1="" subset2="" bin="" out=""
while [ $# -gt 0 ]; do
    case $1 in
        --hs38) hs38=$2; shift 2 ;;
        --reads) reads1=$2 reads2=$3; shift 3 ;;
        --subset) subset1=$2 subset2=$3; shift 3 ;;
        --bin) bin=$2; shift 2 ;;
        --out) out=$2; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$hs38" ] && [ -n "$reads2" ] && [ -n "$subset2" ] && [ -n "$bin" ] && [ -n "$out" ] || usage
command -v samtools > /dev/null || { echo "make_data.sh needs samtools" >&2; exit 1; }
command -v zstd > /dev/null || { echo "make_data.sh needs zstd" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
threads=$(sysctl -n hw.ncpu)
K=10000000
medium_pairs=500000
mkdir -p "$out/data/reads" "$out/assets"
out=$(cd "$out" && pwd)
data=$out/data

sam_hash() { grep -v '^@PG' | shasum -a 256 | cut -c1-16; }

# 1. Where each 5M pair maps on hs38: the primary record of read 1 gives the
#    contig of each mate. The same alignment checks the 5M expected hash.
if [ ! -s "$out/pairs.tsv" ]; then
    "$bin/wgse-bwa" --metal mem -t "$threads" -K "$K" "$hs38/hs38.fa" "$reads1" "$reads2" \
        2> "$out/pairs.stderr" |
        tee >(sam_hash > "$out/full-5m.wgse-bwa.hash") |
        awk -F '\t' '!/^@/ && int($2 / 64) % 2 && int($2 / 256) % 8 == 0 {
            print $1 "\t" $3 "\t" ($7 == "=" ? $3 : $7) }' > "$out/pairs.tsv.partial"
    mv "$out/pairs.tsv.partial" "$out/pairs.tsv"
fi

# subset <name> <contigs (space-separated)> <max pairs>: the pairs whose mates
# both map to the contigs, in input order.
subset() {
    local name=$1 contigs=$2 max=$3 mate source
    [ -s "$data/reads/${name}_R2.fastq.gz" ] && return
    awk -F '\t' -v contigs="$contigs" -v max="$max" '
        BEGIN { n = split(contigs, c, " "); for (i = 1; i <= n; i++) keep[c[i]] = 1 }
        ($2 in keep) && ($3 in keep) && kept < max { print $1; kept++ }' \
        "$out/pairs.tsv" > "$out/$name.names"
    for mate in 1 2; do
        if [ "$mate" = 1 ]; then source=$reads1; else source=$reads2; fi
        gzip -dc "$source" | awk -v names="$out/$name.names" '
            BEGIN { while ((getline line < names) > 0) keep[line] = 1 }
            NR % 4 == 1 { split(substr($0, 2), id, /[ \t]/); take = id[1] in keep }
            take' | gzip -n -6 > "$data/reads/${name}_R$mate.fastq.gz"
    done
    echo "$name: $(wc -l < "$out/$name.names" | tr -d ' ') pairs"
}
subset chr20-hg002 "chr20" 1000000000
subset chr1-3-hg002 "chr1 chr2 chr3" "$medium_pairs"
ln -sf "$subset1" "$data/reads/hg002-200k_R1.fastq.gz"
ln -sf "$subset2" "$data/reads/hg002-200k_R2.fastq.gz"
ln -sf "$reads1" "$data/reads/hg002-5m_R1.fastq.gz"
ln -sf "$reads2" "$data/reads/hg002-5m_R2.fastq.gz"

# 2. The chr20 and chr1-3 references and their indexes.
reference() {
    local name=$1; shift
    mkdir -p "$data/$name"
    [ -s "$data/$name/$name.fa.0123" ] && return
    samtools faidx "$hs38/hs38.fa" "$@" > "$data/$name/$name.fa"
    "$bin/wgse-bwa" index "$data/$name/$name.fa" > "$out/$name.bwa-index.log" 2>&1
    /usr/bin/time -l "$bin/wgse-bwa-mem2" index "$data/$name/$name.fa" \
        > "$out/$name.mem2-index.log" 2>&1
}
reference chr20 chr20
reference chr1-3 chr1 chr2 chr3
mkdir -p "$data/hs38"
for ext in amb ann bwt pac sa bwt.2bit.64 0123; do
    ln -sf "$hs38/hs38.fa.$ext" "$data/hs38/hs38.fa.$ext"
done

# 3. Expected hashes. The CPU backends are upstream's code; for the 5M pairs
#    --metal (byte-identical, and much faster) stands in for them.
expect() {
    local tier=$1 ref=$2 reads=$3 backend=$4 tool file
    for tool in wgse-bwa wgse-bwa-mem2; do
        file=$out/$tier.$tool.hash
        # shellcheck disable=SC2086
        [ -s "$file" ] || "$bin/$tool" $backend mem -t "$threads" -K "$K" "$data/$ref/$ref.fa" \
            "$data/reads/${reads}_R1.fastq.gz" "$data/reads/${reads}_R2.fastq.gz" \
            2> "$out/$tier.$tool.stderr" | sam_hash > "$file"
    done
}
expect small chr20 chr20-hg002 ""
expect medium chr1-3 chr1-3-hg002 ""
expect full hs38 hg002-200k ""
expect full-5m hs38 hg002-5m --metal
{
    printf '# tier\ttool\tK\thash of the SAM without @PG lines (shasum -a 256, first 16)\n'
    for tier in small medium full full-5m; do
        for tool in wgse-bwa wgse-bwa-mem2; do
            [ -s "$out/$tier.$tool.hash" ] && printf '%s\t%s\t%s\t%s\n' "$tier" "$tool" "$K" "$(cat "$out/$tier.$tool.hash")"
        done
    done
} > "$out/expected.tsv"

# 4. Release assets: indexes compressed with zstd, files over 1.9 GB split into
#    numbered parts; reads as they are. data.tsv lists each file of the layout.
part_bytes=1900000000
{
    printf '# path\tbytes\tsha256\tassets (concatenated, then zstd -d if .zst)\n'
    for file in chr20/chr20.fa chr1-3/chr1-3.fa hs38/hs38.fa; do
        for ext in amb ann bwt pac sa bwt.2bit.64 0123; do
            path=$file.$ext
            asset=$(basename "$path").zst
            if ! ls "$out/assets/$asset"* > /dev/null 2>&1; then
                zstd -q -T"$threads" -10 -c "$data/$path" > "$out/assets/$asset"
                if [ "$(stat -f %z "$out/assets/$asset")" -gt "$part_bytes" ]; then
                    split -b "$part_bytes" -d -a 2 "$out/assets/$asset" "$out/assets/$asset."
                    rm "$out/assets/$asset"
                fi
            fi
            assets=$(cd "$out/assets" && ls "$asset"* | paste -sd , -)
            printf '%s\t%s\t%s\t%s\n' "$path" "$(stat -L -f %z "$data/$path")" \
                "$(shasum -a 256 < "$data/$path" | cut -d' ' -f1)" "$assets"
        done
    done
    for reads in chr20-hg002 chr1-3-hg002 hg002-200k hg002-5m; do
        for mate in 1 2; do
            path=reads/${reads}_R$mate.fastq.gz
            [ -e "$out/assets/$(basename "$path")" ] || cp -L "$data/$path" "$out/assets/"
            printf '%s\t%s\t%s\t%s\n' "$path" "$(stat -L -f %z "$data/$path")" \
                "$(shasum -a 256 < "$data/$path" | cut -d' ' -f1)" "$(basename "$path")"
        done
    done
} > "$out/data.tsv"
echo "Wrote $out/data.tsv, $out/expected.tsv and $(ls "$out/assets" | wc -l | tr -d ' ') assets ($(du -sh "$out/assets" | cut -f1))"
echo "Upload: gh release upload <tag> $out/assets/* ; then copy data.tsv and expected.tsv to $here"
