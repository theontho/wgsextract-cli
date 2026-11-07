#!/bin/bash
# Benchmark and parity-test the native aligners on a dataset tier, locally or
# on a GitHub-hosted Mac (.github/workflows/aligner_bench.yml).
#
#   bench.sh fetch --data DIR --tier TIER [--configs LIST] [--repo OWNER/NAME]
#       Download the tier's reference index and reads from the release named in
#       data.tsv into DIR and verify their sha256; files already there and
#       verified are kept.
#   bench.sh run --data DIR --tier TIER --out DIR --bin LABEL=DIR [--bin ...]
#                [--configs LIST] [--rounds N] [--threads N] [--timeout SECONDS]
#                [--balloon MIB] [--name NAME]
#       Run every config with every binary directory (variant) LABEL, in
#       interleaved order (reversed on even rounds), and write DIR/results.tsv,
#       DIR/hwinfo.txt, DIR/summary.md and DIR/runs/*. Exits 1 if any run
#       failed or any alignments differ from the expected ones.
#   bench.sh summary RESULTS.tsv...
#       Print a Markdown table: per name, tier, config and variant, the median
#       of the runs and the change against the first variant.
#   bench.sh hwinfo --out DIR
#       Print the machine's memory, CPU, swap, power and Metal device.
#
# A config is TOOL:BACKEND[:tN][:KN][:probe][:+ARG...]
#   TOOL     bwa (wgse-bwa) or mem2 (wgse-bwa-mem2)
#   BACKEND  cpu, metal (--metal) or auto (--backend auto)
#   tN       threads (default --threads, or every core)
#   KN       -K N (default 10000000; K0 leaves -K out)
#   probe    a failure (crash, kill, timeout) is reported, not failed
#   +ARG     an extra argument for the aligner, for example +--gpu-policy:+all
# Alignments of each tool, tier and K must match expected.tsv when it has them,
# and each other, whatever the backend, threads or variant.
#
# To add a tool, add a case to tool_binary, tool_files and tool_command.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
default_K=10000000

die() { echo "bench.sh: $*" >&2; exit 2; }
usage() { sed -n '2,31p' "$0" >&2; exit 2; }

tool_binary() {
    case $1 in
        bwa) echo wgse-bwa ;;
        mem2) echo wgse-bwa-mem2 ;;
        *) die "unknown tool $1" ;;
    esac
}

# tool_files TOOL BACKEND: the index files after <ref>.fa. that it reads.
tool_files() {
    case $1:$2 in
        bwa:*) echo "amb ann bwt pac sa" ;;
        mem2:metal) echo "amb ann bwt.2bit.64 pac" ;; # --metal does not read the .0123
        mem2:*) echo "amb ann bwt.2bit.64 pac 0123" ;;
    esac
}

# tool_command BIN TOOL BACKEND THREADS K REF.fa R1 R2 [ARG...]: sets cmd.
tool_command() {
    local bin=$1 tool=$2 backend=$3 threads=$4 K=$5 ref=$6 r1=$7 r2=$8
    shift 8
    cmd=("$bin/$(tool_binary "$tool")")
    case $backend in
        cpu) ;;
        metal) cmd+=(--metal) ;;
        auto) cmd+=(--backend auto) ;;
        *) die "unknown backend $backend" ;;
    esac
    cmd+=(mem -t "$threads")
    [ "$K" = 0 ] || cmd+=(-K "$K")
    cmd+=("$@" "$ref" "$r1" "$r2")
}

# parse_config CONFIG: sets c_tool, c_backend, c_threads, c_K, c_probe, c_args.
parse_config() {
    local IFS=: field
    local -a fields
    read -r -a fields <<< "$1"
    [ ${#fields[@]} -ge 2 ] || die "config $1 is not TOOL:BACKEND[:...]"
    c_tool=${fields[0]} c_backend=${fields[1]} c_threads=$threads c_K=$default_K c_probe=0
    c_args=()
    tool_binary "$c_tool" > /dev/null
    case $c_backend in cpu | metal | auto) ;; *) die "unknown backend in $1" ;; esac
    for field in "${fields[@]:2}"; do
        case $field in
            t[0-9]*) c_threads=${field#t} ;;
            K[0-9]*) c_K=${field#K} ;;
            probe) c_probe=1 ;;
            +*) c_args+=("${field#+}") ;;
            *) die "unknown field $field in config $1" ;;
        esac
    done
}

check_configs() {
    local config
    for config in ${1//,/ }; do parse_config "$config"; done
}

# tier_field TIER COLUMN
tier_field() {
    awk -F '\t' -v tier="$1" -v column="$2" '!/^#/ && $1 == tier { print $column; found = 1 }
        END { exit !found }' "$here/tiers.tsv" || die "unknown tier $1 (see tiers.tsv)"
}

sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
file_bytes() { stat -L -f %z "$1" 2> /dev/null || stat -L -c %s "$1"; }

# The data files a tier and its configs need, relative to the data directory.
tier_paths() {
    local tier=$1 configs=$2 ref reads config ext
    ref=$(tier_field "$tier" 2)
    reads=$(tier_field "$tier" 3)
    {
        for config in ${configs//,/ }; do
            parse_config "$config"
            for ext in $(tool_files "$c_tool" "$c_backend"); do echo "$ref/$ref.fa.$ext"; done
        done
        echo "reads/${reads}_R1.fastq.gz"
        echo "reads/${reads}_R2.fastq.gz"
    } | awk '!seen[$0]++'
}

fetch() {
    local data="" tier="" configs="" repo=WGSExtract/wgsextract-cli tag path bytes sha assets
    while [ $# -gt 0 ]; do
        case $1 in
            --data) data=$2; shift 2 ;;
            --tier) tier=$2; shift 2 ;;
            --configs) configs=$2; shift 2 ;;
            --repo) repo=$2; shift 2 ;;
            *) usage ;;
        esac
    done
    [ -n "$data" ] && [ -n "$tier" ] || usage
    threads=1
    [ -n "$configs" ] || configs=$(tier_field "$tier" 4)
    check_configs "$configs"
    tag=$(awk '/^# tag / { print $3 }' "$here/data.tsv")
    mkdir -p "$data/.download"
    local missing=() line
    for path in $(tier_paths "$tier" "$configs"); do
        line=$(awk -F '\t' -v path="$path" '$1 == path' "$here/data.tsv")
        [ -n "$line" ] || die "data.tsv has no $path"
        IFS=$'\t' read -r path bytes sha assets <<< "$line"
        if [ -f "$data/$path.sha256" ] && [ "$(cat "$data/$path.sha256")" = "$sha" ] &&
            [ "$(file_bytes "$data/$path")" = "$bytes" ]; then
            continue
        fi
        missing+=("$path	$sha	$assets")
    done
    [ ${#missing[@]} -gt 0 ] || { echo "fetch: $tier is already in $data"; return; }
    local started=$SECONDS total=0 entry attempt part
    for entry in "${missing[@]}"; do
        IFS=$'\t' read -r path sha assets <<< "$entry"
        for attempt in 1 2 3; do
            rm -f "$data/$path.sha256"
            # Download the parts in parallel, with retries that resume.
            tr , '\n' <<< "$assets" | xargs -P 4 -I {} curl -fsSL --retry 8 --retry-all-errors \
                -C - -o "$data/.download/{}" "https://github.com/$repo/releases/download/$tag/{}" ||
                { echo "fetch: download of $path failed (attempt $attempt)" >&2; continue; }
            mkdir -p "$(dirname "$data/$path")"
            local parts=()
            for part in ${assets//,/ }; do parts+=("$data/.download/$part"); done
            case $assets in
                *.zst | *.zst.[0-9][0-9]*) cat "${parts[@]}" | zstd -q -d -c > "$data/$path" ;;
                *) cat "${parts[@]}" > "$data/$path" ;;
            esac
            for part in "${parts[@]}"; do total=$((total + $(file_bytes "$part"))); done
            rm -f "${parts[@]}"
            if [ "$(sha256 "$data/$path")" = "$sha" ]; then
                echo "$sha" > "$data/$path.sha256"
                break
            fi
            echo "fetch: $path has the wrong sha256 (attempt $attempt)" >&2
        done
        [ -f "$data/$path.sha256" ] || { echo "fetch: could not fetch $path" >&2; exit 1; }
        echo "fetch: $path"
    done
    echo "fetch: $tier: $((total / 1048576)) MiB in $((SECONDS - started)) s"
}

build_helpers() {
    local dir=$1
    mkdir -p "$dir"
    [ -x "$dir/runstat" ] || cc -O2 -o "$dir/runstat" "$here/runstat.c"
    [ -x "$dir/balloon" ] || cc -O2 -o "$dir/balloon" "$here/balloon.c"
    if [ ! -x "$dir/metal_info" ] && command -v swiftc > /dev/null; then
        swiftc -O -o "$dir/metal_info" "$here/metal_info.swift" > /dev/null 2>&1 || true
    fi
}

hwinfo() {
    local out="" name
    while [ $# -gt 0 ]; do
        case $1 in
            --out) out=$2; shift 2 ;;
            *) usage ;;
        esac
    done
    [ -n "$out" ] || usage
    build_helpers "$out/helpers"
    echo "os=$(sw_vers -productVersion) ($(sw_vers -buildVersion))"
    for name in hw.model machdep.cpu.brand_string hw.ncpu hw.perflevel0.logicalcpu \
        hw.perflevel1.logicalcpu hw.memsize vm.swapusage vm.user_wire_limit \
        kern.memorystatus_level kern.memorystatus_vm_pressure_level; do
        echo "$name=$(sysctl -n "$name" 2> /dev/null || echo -)"
    done
    echo "disk_free_gib=$(df -k "$out" | awk 'NR == 2 { printf "%.1f", $4 / 1048576 }')"
    echo "power=$(pmset -g ps 2> /dev/null | head -1 | sed "s/^Now drawing from //; s/'//g")"
    echo "thermal=$(pmset -g therm 2> /dev/null | grep -i 'warning\|limit' | tr '\n' ';' | sed 's/;$//')"
    [ -x "$out/helpers/metal_info" ] && "$out/helpers/metal_info" || echo "metal_device=unknown"
}

# The background memory sampler: time, swap used (MiB), pressure level
# (1 normal, 2 warning, 4 critical), memorystatus level (% free) and the
# compressor's size (MiB), every two seconds.
sampler() {
    local page
    page=$(sysctl -n hw.pagesize)
    while :; do
        printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%s)" \
            "$(sysctl -n vm.swapusage | awk '{ for (i = 1; i < NF; i++) if ($i == "used") {
                v = $(i + 2) + 0; if ($(i + 2) ~ /G/) v *= 1024; print v } }')" \
            "$(sysctl -n kern.memorystatus_vm_pressure_level)" "$(sysctl -n kern.memorystatus_level)" \
            "$(vm_stat | awk -v page="$page" '/occupied by compressor/ { printf "%.0f", $NF * page / 1048576 }')"
        sleep 2
    done
}

vm_counters() {
    vm_stat | awk '/^(Swapins|Swapouts|Pageouts|Pageins|Compressions|Decompressions):/ {
        sub(/:/, "", $1); sub(/\./, "", $2); print $1 "=" $2 }'
}

# Read the run's index and reads, so that no run pays for the disk because
# another config or variant ran before it, unless they could not stay cached.
warm() {
    local files=() ext total=0 file
    for ext in $(tool_files "$c_tool" "$c_backend"); do files+=("$ref_fa.$ext"); done
    files+=("$data/reads/${reads}_R1.fastq.gz" "$data/reads/${reads}_R2.fastq.gz")
    for file in "${files[@]}"; do total=$((total + $(file_bytes "$file"))); done
    [ "$total" -lt $((($(sysctl -n hw.memsize) - balloon * 1048576) / 2)) ] && cat "${files[@]}" > /dev/null || true
}

# One run: writes $dir/runstat.txt, stderr.txt, samples.tsv, vm_*.txt and
# hash, and prints its results.tsv row.
run_one() {
    local dir=$1 label=$2 bin=$3 config=$4 round=$5 rc sampler_pid hash status expected
    mkdir -p "$dir"
    parse_config "$config"
    tool_command "$bin" "$c_tool" "$c_backend" "$c_threads" "$c_K" "$ref_fa" \
        "$data/reads/${reads}_R1.fastq.gz" "$data/reads/${reads}_R2.fastq.gz" ${c_args[@]+"${c_args[@]}"}
    printf '%q ' "${cmd[@]}" > "$dir/command.txt"
    warm
    vm_counters > "$dir/vm_before.txt"
    date '+%Y-%m-%d %H:%M:%S' > "$dir/started.txt"
    sampler > "$dir/samples.tsv" 2> /dev/null &
    sampler_pid=$!
    set +e
    "$helpers/runstat" -t "$timeout" "$dir/runstat.txt" "${cmd[@]}" 2> "$dir/stderr.txt" |
        grep -v '^@PG' | shasum -a 256 | cut -c1-16 > "$dir/hash"
    rc=${PIPESTATUS[0]}
    set -e
    kill "$sampler_pid" 2> /dev/null || true
    wait "$sampler_pid" 2> /dev/null || true
    vm_counters > "$dir/vm_after.txt"
    hash=$(cat "$dir/hash")
    expected=$(awk -F '\t' -v tier="$tier" -v tool="$(tool_binary "$c_tool")" -v K="$c_K" \
        '!/^#/ && $1 == tier && $2 == tool && $3 == K { print $4 }' "$here/expected.tsv")
    if [ "$rc" != 0 ]; then
        diagnose "$dir"
        if [ "$c_probe" = 1 ]; then status=probe-failed; else status=failed; fi
    elif [ -n "$expected" ] && [ "$hash" != "$expected" ]; then
        status=mismatch
    else
        status=pass
    fi
    row "$dir" "$label" "$config" "$round" "$rc" "$hash" "${expected:--}" "$status"
}

# After a failed run: the kernel's memorystatus (jetsam) messages and any new
# crash or jetsam reports.
diagnose() {
    local dir=$1 report
    log show --style compact --start "$(cat "$dir/started.txt")" \
        --predicate 'eventMessage CONTAINS[c] "memorystatus" OR eventMessage CONTAINS[c] "jetsam" OR eventMessage CONTAINS[c] "low swap"' \
        2> /dev/null | tail -200 > "$dir/memorystatus.log" || true
    for report in /Library/Logs/DiagnosticReports/* "$HOME"/Library/Logs/DiagnosticReports/*; do
        [ -f "$report" ] && [ "$report" -nt "$dir/started.txt" ] && cp "$report" "$dir/" || true
    done
}

results_header() {
    printf 'name\ttier\tlabel\tconfig\tround\texit\tsignal\ttimed_out\treal_s\tuser_s\tsys_s\tmax_rss_mib\tfootprint_mib\tpage_faults\tpageins\tswap_max_mib\tswapout_mib\tcompressor_max_mib\tpressure_max\tfree_min_pct\tgpu_batches\tcpu_fallback\tmetal_failed\tinterruptions\tjetsam\thash\texpected\tstatus\tnote\n'
}

row() {
    local dir=$1 label=$2 config=$3 round=$4 rc=$5 hash=$6 expected=$7 status=$8 note jetsam=0
    note=$(grep -m 1 -o 'CPU fallback: .*\|Metal execution failed.*\|[Ee]rror[: ].*\|[Ff]ailed.*' "$dir/stderr.txt" |
        tr '\t' ' ' | cut -c1-120 || true)
    if [ -s "$dir/memorystatus.log" ] && grep -qi "killing.*wgse\|wgse.*kill" "$dir/memorystatus.log"; then
        jetsam=1
    fi
    ls "$dir"/JetsamEvent* > /dev/null 2>&1 && jetsam=1
    awk -F '=' -v OFS='\t' -v name="$name" -v tier="$tier" -v label="$label" -v config="$config" \
        -v round="$round" -v rc="$rc" -v hash="$hash" -v expected="$expected" -v status="$status" \
        -v note="${note:--}" -v jetsam="$jetsam" -v page="$(sysctl -n hw.pagesize)" \
        -v samples="$dir/samples.tsv" -v before="$dir/vm_before.txt" -v after="$dir/vm_after.txt" \
        -v stderr="$dir/stderr.txt" '
        BEGIN { free_min = 100 }
        FILENAME == before { vm0[$1] = $2; next }
        FILENAME == after { vm1[$1] = $2; next }
        FILENAME == samples { split($0, s, "\t"); sampled++; if (s[2] > swap) swap = s[2]
            if (s[3] > pressure) pressure = s[3]; if (s[4] != "" && s[4] < free_min) free_min = s[4]
            if (s[5] > compressor) compressor = s[5]; next }
        FILENAME == stderr { line = $0
            while (match(line, /(gpu|seed|rescue|extend)_batches=[0-9]+/)) {
                v = substr(line, RSTART, RLENGTH); sub(/.*=/, "", v); gpu += v; line = substr(line, RSTART + RLENGTH) }
            if (match($0, /interruptions=[0-9]+/)) { v = substr($0, RSTART, RLENGTH); sub(/.*=/, "", v); interruptions += v }
            if ($0 ~ /CPU fallback/) fallback++
            if ($0 ~ /Metal execution failed/) failed++
            next }
        { r[$1] = $2 }
        END {
            if (rc != 0 && hash == expected) note = (note == "-" ? "" : note "; ") "the alignments are complete and match"
            print name, tier, label, config, round, rc, r["signal"] + 0, r["timed_out"] + 0,
                r["real_s"], r["user_s"], r["sys_s"],
                sprintf("%.0f", r["max_rss_bytes"] / 1048576), sprintf("%.0f", r["peak_footprint_bytes"] / 1048576),
                r["page_faults"] + 0, r["pageins"] + 0, swap + 0, sprintf("%.0f", (vm1["Swapouts"] - vm0["Swapouts"]) * page / 1048576),
                compressor + 0, pressure + 0, sampled ? free_min : "-", gpu + 0, fallback + 0, failed + 0, interruptions + 0,
                jetsam, hash, expected, status, note }
    ' "$dir/runstat.txt" "$dir/vm_before.txt" "$dir/vm_after.txt" "$dir/samples.tsv" "$dir/stderr.txt"
}

run() {
    local out="" configs="" rounds=1 labels=() bins=() spec
    data="" tier="" timeout=7200 threads=$(sysctl -n hw.ncpu) name=$(hostname -s) balloon=0
    while [ $# -gt 0 ]; do
        case $1 in
            --data) data=$2; shift 2 ;;
            --tier) tier=$2; shift 2 ;;
            --out) out=$2; shift 2 ;;
            --bin) spec=$2; labels+=("${spec%%=*}"); bins+=("$(cd "${spec#*=}" && pwd)"); shift 2 ;;
            --configs) configs=$2; shift 2 ;;
            --rounds) rounds=$2; shift 2 ;;
            --threads) threads=$2; shift 2 ;;
            --timeout) timeout=$2; shift 2 ;;
            --balloon) balloon=$2; shift 2 ;;
            --name) name=$2; shift 2 ;;
            *) usage ;;
        esac
    done
    [ -n "$data" ] && [ -n "$tier" ] && [ -n "$out" ] && [ ${#bins[@]} -gt 0 ] || usage
    [ -n "$configs" ] || configs=$(tier_field "$tier" 4)
    check_configs "$configs"
    mkdir -p "$out/runs"
    out=$(cd "$out" && pwd)
    data=$(cd "$data" && pwd)
    helpers=$out/helpers
    local ref config path i n=0 round order=()
    ref=$(tier_field "$tier" 2)
    reads=$(tier_field "$tier" 3)
    ref_fa=$data/$ref/$ref.fa
    for path in $(tier_paths "$tier" "$configs"); do
        [ -e "$data/$path" ] || die "$data/$path is missing; run bench.sh fetch"
    done
    for i in "${!bins[@]}"; do
        for config in ${configs//,/ }; do
            parse_config "$config"
            [ -x "${bins[$i]}/$(tool_binary "$c_tool")" ] ||
                die "${bins[$i]} has no $(tool_binary "$c_tool")"
        done
    done
    hwinfo --out "$out" | tee "$out/hwinfo.txt"
    for i in "${!bins[@]}"; do
        for config in ${configs//,/ }; do
            parse_config "$config"
            "${bins[$i]}/$(tool_binary "$c_tool")" capabilities --json \
                > "$out/capabilities-${labels[$i]}-$c_tool.json" 2>&1 || true
        done
    done

    local balloon_pid=""
    if [ "$balloon" -gt 0 ]; then
        "$helpers/balloon" "$balloon" > "$out/balloon.txt" 2>&1 &
        balloon_pid=$!
        trap 'kill "$balloon_pid" 2> /dev/null || true' EXIT
        for i in $(seq 120); do
            grep -q wired "$out/balloon.txt" 2> /dev/null && break
            kill -0 "$balloon_pid" 2> /dev/null || break
            sleep 1
        done
        kill -0 "$balloon_pid" 2> /dev/null || { cat "$out/balloon.txt" >&2; die "the balloon could not wire $balloon MiB"; }
        echo "balloon: $(cat "$out/balloon.txt")"
    fi

    for config in ${configs//,/ }; do
        for i in "${!bins[@]}"; do order+=("$i	$config"); done
    done
    results_header > "$out/results.tsv"
    local entry k count=${#order[@]} slug label
    for round in $(seq "$rounds"); do
        for k in $(seq 0 $((count - 1))); do
            if [ $((round % 2)) = 0 ]; then entry=${order[$((count - 1 - k))]}; else entry=${order[$k]}; fi
            i=${entry%%	*} config=${entry#*	}
            label=${labels[$i]}
            n=$((n + 1))
            slug=$(printf '%03d-%s-%s-r%s' "$n" "$label" "$config" "$round" | tr -c 'A-Za-z0-9.\n-' _)
            echo "== $name $tier round $round: $label $config"
            run_one "$out/runs/$slug" "$label" "${bins[$i]}" "$config" "$round" | tee -a "$out/results.tsv" |
                awk -F '\t' '{ printf "   %s s, max RSS %s MiB, swap %s MiB, exit %s, %s %s\n", $9, $12, $16, $6, $28, $26 }'
        done
    done
    [ -z "$balloon_pid" ] || { kill "$balloon_pid" 2> /dev/null || true; trap - EXIT; }

    # Runs of a tool on a tier with the same K must all agree.
    awk -F '\t' -v OFS='\t' '
        NR == FNR { if (FNR > 1 && $6 == 0) { split($4, f, ":"); K = 10000000
                for (j in f) if (f[j] ~ /^K[0-9]+$/) K = substr(f[j], 2)
                key = f[1] ":" K; if (!(key in first)) first[key] = $26; else if ($26 != first[key]) bad[key] = 1 }
            next }
        FNR > 1 { split($4, f, ":"); K = 10000000; for (j in f) if (f[j] ~ /^K[0-9]+$/) K = substr(f[j], 2)
            if ((f[1] ":" K) in bad && $28 == "pass") $28 = "inconsistent" }
        { print }' "$out/results.tsv" "$out/results.tsv" > "$out/results.tsv.new"
    mv "$out/results.tsv.new" "$out/results.tsv"
    summary "$out/results.tsv" > "$out/summary.md"
    cat "$out/summary.md"
    ! awk -F '\t' 'NR > 1 && $28 != "pass" && $28 != "probe-failed" { bad = 1 } END { exit !bad }' "$out/results.tsv"
}

summary() {
    [ $# -gt 0 ] || usage
    awk -F '\t' '
        function median(list,    n, v, i, j, t) {
            n = split(list, v, " ")
            if (n == 0) return ""
            for (i = 2; i <= n; i++) for (j = i; j > 1 && v[j - 1] + 0 > v[j] + 0; j--) { t = v[j]; v[j] = v[j - 1]; v[j - 1] = t }
            return n % 2 ? v[(n + 1) / 2] : (v[n / 2] + v[n / 2 + 1]) / 2
        }
        function fmt(value, format, scale) { return value == "" ? "-" : sprintf(format, value / scale) }
        FNR == 1 { next }
        {
            group = $1 SUBSEP $2 SUBSEP $4
            key = group SUBSEP $3
            if (!(key in runs)) { keys[++nkeys] = key; if (!(group in base)) base[group] = $3 }
            runs[key]++
            statuses[key] = statuses[key] (index(" " statuses[key] " ", " " $28 " ") ? "" : (statuses[key] == "" ? "" : " ") $28)
            if ($6 == 0) { real[key] = real[key] " " $9; cpu[key] = cpu[key] " " ($10 + $11)
                round_real[key, $5] = $9; rounds[key] = rounds[key] " " $5
                rss[key] = rss[key] " " $12; foot[key] = foot[key] " " $13 }
            else { failures[key] = failures[key] (failures[key] == "" ? "" : "; ") (($8 ? "timeout" : ($7 ? "signal " $7 : "exit " $6)) ($25 ? " (jetsam)" : "")) }
            if ($16 > swap[key]) swap[key] = $16
            if ($17 > swapout[key]) swapout[key] = $17
            if ($19 > pressure[key]) pressure[key] = $19
            if ($20 != "-" && (!(key in freemin) || $20 < freemin[key])) freemin[key] = $20
            gpu[key] += $21; fallback[key] += $22 + $23; interruptions[key] += $24
            if ($6 == 0 || !(key in hash)) hash[key] = $26
            if ($28 != "pass" && $28 != "probe-failed" || note[key] == "") if ($29 != "-") note[key] = $29
        }
        END {
            print "| Runner | Tier | Config | Variant | Runs | Status | Time s | vs first | CPU s | Max RSS GiB | Footprint GiB | Swap GiB | Swapped out GiB | Pressure | Min free | GPU batches | Fallbacks | Hash | Notes |"
            print "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- | --- |"
            for (k = 1; k <= nkeys; k++) {
                key = keys[k]; split(key, p, SUBSEP); group = p[1] SUBSEP p[2] SUBSEP p[3]
                t = median(real[key]); b = median(real[group SUBSEP base[group]])
                # Against the first variant: the median of the ratios of runs in the same round,
                # which ran next to each other, and their range.
                vs = ""; ratios = ""; lo = ""; hi = ""
                if (base[group] != p[4]) {
                    n = split(rounds[key], r, " ")
                    for (i = 1; i <= n; i++) if ((group SUBSEP base[group], r[i]) in round_real) {
                        ratio = round_real[key, r[i]] / round_real[group SUBSEP base[group], r[i]]
                        ratios = ratios " " ratio
                        if (lo == "" || ratio < lo) lo = ratio
                        if (hi == "" || ratio > hi) hi = ratio
                    }
                    if (ratios != "") {
                        vs = sprintf("%+.1f%%", (median(ratios) - 1) * 100)
                        if (lo != hi) vs = vs sprintf(" (%+.0f%% to %+.0f%%)", (lo - 1) * 100, (hi - 1) * 100)
                    } else if (t != "" && b != "") vs = sprintf("%+.1f%%", (t / b - 1) * 100)
                }
                level = "normal"
                if (pressure[key] >= 2) level = "warning"
                if (pressure[key] >= 4) level = "critical"
                notes = failures[key]
                if (notes != "" && note[key] != "") notes = notes "; "
                notes = notes note[key]
                gsub(/\|/, "/", notes)
                printf "| %s | %s | `%s` | %s | %d | %s | %s | %s | %s | %s | %s | %.2f | %.2f | %s | %s | %d | %d | %s | %s |\n",
                    p[1], p[2], p[3], p[4], runs[key], statuses[key], fmt(t, "%.1f", 1), vs,
                    fmt(median(cpu[key]), "%.0f", 1), fmt(median(rss[key]), "%.2f", 1024),
                    fmt(median(foot[key]), "%.2f", 1024), swap[key] / 1024, swapout[key] / 1024, level,
                    (key in freemin) ? freemin[key] "%" : "-", gpu[key], fallback[key], (key in hash) ? "`" hash[key] "`" : "-", notes
            }
        }' "$@"
}

[ $# -gt 0 ] || usage
command=$1
shift
case $command in
    fetch) fetch "$@" ;;
    run) run "$@" ;;
    summary) summary "$@" ;;
    hwinfo) hwinfo "$@" ;;
    *) usage ;;
esac
