// BWA 0.7.19's bidirectional FM-index traversal, three-pass MEM collection and
// suffix-array locate. Adapted from bwt.c/bwamem.c (Heng Li et al., MIT; see SEEDING-LICENSE).
// seeding_types.h is prepended by CMake for runtime compilation.

struct SeedVector {
    device SeedInterval *data;
    uint stride, size, capacity;

    SeedInterval get(uint i) const { return data[ulong(i) * stride]; }
    void set(uint i, SeedInterval value) { data[ulong(i) * stride] = value; }
    bool push(SeedInterval value) {
        if (size == capacity) return false;
        set(size++, value);
        return true;
    }
    void reverse() {
        for (uint i = 0; i < size / 2; ++i) {
            SeedInterval value = get(i);
            set(i, get(size - 1 - i));
            set(size - 1 - i, value);
        }
    }
};

// Occurrences within a 128-symbol block, read as four 64-bit lanes (words 2i and
// 2i + 1). Symbol j of a 32-bit word is bits 31 - 2j (high) and 30 - 2j (low).
// Every lane is counted, with a mask of the kept symbols' low bits: on the GPU
// this branchless form is faster than looping over the words before k.
ulong seed_keep(uint lane, ulong k) {
    uint word = uint(k & 127) >> 4;
    uint partial = ~((1u << ((~uint(k) & 15) * 2)) - 1) & 0x55555555u;
    uint lo = 2 * lane < word ? 0x55555555u : 2 * lane == word ? partial : 0u;
    uint hi = 2 * lane + 1 < word ? 0x55555555u : 2 * lane + 1 == word ? partial : 0u;
    return ulong(lo) | (ulong(hi) << 32);
}

// Counts of A, C, G and T up to and including symbol k & 127 of its block;
// masked-out symbols read as A, so A is derived from the kept total.
ulong4 seed_count(device const ulong *data, ulong k) {
    ulong c = 0, g = 0, t = 0;
    for (uint lane = 0; lane < 4; ++lane) {
        ulong keep = seed_keep(lane, k), lo = data[lane] & keep, hi = (data[lane] >> 1) & keep;
        c += popcount(lo & ~hi);
        g += popcount(hi & ~lo);
        t += popcount(lo & hi);
    }
    return ulong4((k & 127) + 1 - c - g - t, c, g, t);
}

ulong4 seed_occ4(device const uint *bwt, ulong k, constant SeedParameters &p) {
    if (k == ulong(-1)) return ulong4(0);
    k -= k >= p.primary;
    device const uint *block = bwt + (k >> 7) * 16;
    return *reinterpret_cast<device const ulong4 *>(block)
        + seed_count(reinterpret_cast<device const ulong *>(block + 8), k);
}

void seed_2occ4(device const uint *bwt, ulong k, ulong l,
    constant SeedParameters &p, thread ulong4 &left, thread ulong4 &right) {
    left = seed_occ4(bwt, k, p);
    right = seed_occ4(bwt, l, p);
}

SeedInterval seed_initial(uint c, constant SeedParameters &p) {
    return {{p.l2[c] + 1, p.l2[3 - c] + 1, p.l2[c + 1] - p.l2[c]}, 0};
}

SeedInterval seed_extend(device const uint *bwt, SeedInterval interval,
    uint c, bool back, constant SeedParameters &p) {
    ulong4 left, right;
    uint side = !back;
    ulong begin = interval.x[side];
    seed_2occ4(bwt, begin - 1, begin - 1 + interval.x[2], p, left, right);
    ulong4 sizes = right - left;
    SeedInterval out = interval;
    out.x[side] = p.l2[c] + 1 + left[c];
    out.x[2] = sizes[c];
    out.x[back] += begin <= p.primary && begin + interval.x[2] - 1 >= p.primary;
    for (uint i = c + 1; i < 4; ++i) out.x[back] += sizes[i];
    return out;
}

// mem_collect_intv always calls bwt_smem1a with max_intv == 0.
int seed_smem(device const uint *bwt, device const uchar *q, int len, int x,
    ulong minimum, constant SeedParameters &p, thread SeedVector &mem,
    SeedVector prev, SeedVector curr) {
    mem.size = 0;
    if (q[x] > 3) return x + 1;
    minimum = max(minimum, 1ul);
    SeedInterval ik = seed_initial(q[x], p);
    ik.info = x + 1;
    int i;
    curr.size = 0;
    for (i = x + 1; i < len; ++i) {
        if (q[i] < 4) {
            SeedInterval next = seed_extend(bwt, ik, 3 - q[i], false, p);
            if (next.x[2] != ik.x[2]) {
                if (!curr.push(ik)) return -1;
                if (next.x[2] < minimum) break;
            }
            ik = next;
            ik.info = i + 1;
        } else {
            if (!curr.push(ik)) return -1;
            break;
        }
    }
    if (i == len && !curr.push(ik)) return -1;
    curr.reverse();
    int ret = int(curr.get(0).info);
    SeedVector swap = curr;
    curr = prev;
    prev = swap;
    for (i = x - 1; i >= -1; --i) {
        int c = i < 0 ? -1 : q[i] < 4 ? q[i] : -1;
        curr.size = 0;
        for (uint j = 0; j < prev.size; ++j) {
            SeedInterval candidate = prev.get(j), next;
            if (c >= 0) next = seed_extend(bwt, candidate, uint(c), true, p);
            if (c < 0 || next.x[2] < minimum) {
                if (curr.size == 0 &&
                    (mem.size == 0 || ulong(i + 1) < (mem.get(mem.size - 1).info >> 32))) {
                    candidate.info |= ulong(i + 1) << 32;
                    if (!mem.push(candidate)) return -1;
                }
            } else if (curr.size == 0 || next.x[2] != curr.get(curr.size - 1).x[2]) {
                next.info = candidate.info;
                if (!curr.push(next)) return -1;
            }
        }
        if (curr.size == 0) break;
        swap = curr;
        curr = prev;
        prev = swap;
    }
    mem.reverse();
    return ret;
}

bool seed_append_long(thread SeedVector &out, SeedVector mem, int min_len) {
    for (uint i = 0; i < mem.size; ++i) {
        SeedInterval interval = mem.get(i);
        if (uint(interval.info) - (interval.info >> 32) >= ulong(min_len)
            && !out.push(interval)) return false;
    }
    return true;
}

kernel void collect_seeds(device const uint *bwt [[buffer(0)]],
    constant SeedParameters &p [[buffer(1)]],
    device const uchar *sequences [[buffer(2)]],
    device const SeedRead *reads [[buffer(3)]],
    device SeedInterval *scratch [[buffer(4)]],
    device SeedInterval *output [[buffer(5)]],
    device SeedResult *results [[buffer(6)]],
    uint tid [[thread_position_in_grid]]) {
    if (tid >= p.count) return;
    results[tid] = {0, seed_ok};
    const int len = int(reads[tid].length);
    if (len > int(seed_max_length)) {
        results[tid].status = seed_length_limit;
        return;
    }
    device const uchar *q = sequences + reads[tid].offset;
    ulong region = ulong(seed_scratch_capacity) * p.count;
    SeedVector mem = {scratch + tid, p.count, 0, p.scratch_limit};
    SeedVector prev = {scratch + region + tid, p.count, 0, p.scratch_limit};
    SeedVector curr = {scratch + 2 * region + tid, p.count, 0, p.scratch_limit};
    // Scratch remains lane-interleaved; final output is contiguous per read so
    // CPU consumers can copy intervals without a strided cache/TLB walk.
    SeedVector out = {output + ulong(tid) * seed_output_capacity, 1, 0, p.output_limit};
    for (int x = 0; x < len;) {
        if (q[x] >= 4) { ++x; continue; }
        x = seed_smem(bwt, q, len, x, 1, p, mem, prev, curr);
        if (x < 0) { results[tid].status = seed_scratch_overflow; return; }
        if (!seed_append_long(out, mem, p.min_seed_len)) {
            results[tid].status = seed_output_overflow; return;
        }
    }
    uint old_size = out.size;
    for (uint k = 0; k < old_size; ++k) {
        SeedInterval interval = out.get(k);
        int start = int(interval.info >> 32), end = int(interval.info);
        if (end - start < p.split_len || interval.x[2] > ulong(p.split_width)) continue;
        if (seed_smem(bwt, q, len, (start + end) >> 1, interval.x[2] + 1,
            p, mem, prev, curr) < 0) {
            results[tid].status = seed_scratch_overflow; return;
        }
        if (!seed_append_long(out, mem, p.min_seed_len)) {
            results[tid].status = seed_output_overflow; return;
        }
    }
    if (p.max_mem_intv > 0) {
        for (int x = 0; x < len;) {
            if (q[x] >= 4) { ++x; continue; }
            SeedInterval interval = seed_initial(q[x], p);
            int i;
            for (i = x + 1; i < len; ++i) {
                if (q[i] >= 4) break;
                interval = seed_extend(bwt, interval, 3 - q[i], false, p);
                if (interval.x[2] < ulong(p.max_mem_intv) && i - x >= p.min_seed_len) {
                    interval.info = (ulong(x) << 32) | uint(i + 1);
                    if (interval.x[2] > 0 && !out.push(interval)) {
                        results[tid].status = seed_output_overflow; return;
                    }
                    break;
                }
            }
            x = i == len ? len : i + 1;
        }
    }
    for (uint i = 1; i < out.size; ++i) {
        SeedInterval value = out.get(i);
        uint j = i;
        while (j > 0 && value.info < out.get(j - 1).info) {
            out.set(j, out.get(j - 1));
            --j;
        }
        out.set(j, value);
    }
    results[tid].count = out.size;
}

// bwt_sa(): walk inverse-Psi (LF) steps to the next sampled suffix-array row.
// Mirrors bwt_invPsi/bwt_occ/bwt_B0 in bwt.c exactly, including sa[0] == -1.
ulong locate_occ(device const uint *bwt, ulong k, uint c, constant LocateParameters &p) {
    if (k == p.seq_len) return p.l2[c + 1] - p.l2[c];
    k -= k >= p.primary;
    device const uint *block = bwt + (k >> 7) * 16;
    device const ulong *data = reinterpret_cast<device const ulong *>(block + 8);
    ulong pattern = ulong(c) * 0x5555555555555555ul, count = 0;
    for (uint lane = 0; lane < 4; ++lane) {
        ulong x = data[lane] ^ pattern;
        count += popcount(~(x | (x >> 1)) & seed_keep(lane, k));
    }
    return reinterpret_cast<device const ulong *>(block)[c] + count;
}

// Walks LF from row k until (k & mask) == 0 and returns the number of steps.
ulong locate_walk(device const uint *bwt, thread ulong &k, ulong mask,
    constant LocateParameters &p) {
    ulong steps = 0;
    while (k & mask) {
        ++steps;
        if (k == p.primary) {
            k = 0;
            continue;
        }
        ulong x = k - (k > p.primary);
        uint c = (bwt[(x >> 7 << 4) + 8 + ((x & 127) >> 4)] >> ((~uint(x) & 15) << 1)) & 3;
        k = p.l2[c] + locate_occ(bwt, k, c, p);
    }
    return steps;
}

kernel void locate_rows(device const uint *bwt [[buffer(0)]],
    device const ulong *sa [[buffer(1)]],
    constant LocateParameters &p [[buffer(2)]],
    device const ulong *rows [[buffer(3)]],
    device ulong *positions [[buffer(4)]],
    uint id [[thread_position_in_grid]]) {
    if (id >= p.count) return;
    ulong k = rows[id];
    ulong steps = locate_walk(bwt, k, p.sa_intv - 1, p);
    positions[id] = steps + sa[k >> p.sa_shift];
}

// Denser suffix-array samples, derived from the index: the rows that are
// multiples of 1 << dense_shift but not of sa_intv, (sa_intv >> dense_shift) - 1
// per index sample. Values (< 2^33) are stored as low words plus a bit vector
// of high bits. LF walks stop at the first sample of either kind.
kernel void locate_rows_dense(device const uint *bwt [[buffer(0)]],
    device const ulong *sa [[buffer(1)]],
    constant LocateParameters &p [[buffer(2)]],
    device const ulong *rows [[buffer(3)]],
    device ulong *positions [[buffer(4)]],
    device const uint *low [[buffer(5)]],
    device const uint *high [[buffer(6)]],
    uint id [[thread_position_in_grid]]) {
    if (id >= p.count) return;
    ulong k = rows[id], base = p.sa_intv - 1;
    ulong steps = locate_walk(bwt, k, (1ul << p.dense_shift) - 1, p);
    if (!(k & base)) {
        positions[id] = steps + sa[k >> p.sa_shift];
        return;
    }
    ulong per = (p.sa_intv >> p.dense_shift) - 1;
    ulong i = (k >> p.sa_shift) * per + ((k & base) >> p.dense_shift) - 1;
    positions[id] = steps + (low[i] | (ulong((high[i >> 5] >> (i & 31)) & 1) << 32));
}

// Builds the dense samples, one per thread, in dispatches of count threads
// from entry dense_offset (a multiple of 32, so each SIMD group owns one word of
// high bits). Entries for rows past seq_len do not exist and are left zero.
kernel void locate_dense_build(device const uint *bwt [[buffer(0)]],
    device const ulong *sa [[buffer(1)]],
    constant LocateParameters &p [[buffer(2)]],
    device uint *low [[buffer(3)]],
    device uint *high [[buffer(4)]],
    uint id [[thread_position_in_grid]],
    uint lane [[thread_index_in_simdgroup]]) {
    ulong i = ulong(id) + p.dense_offset, per = (p.sa_intv >> p.dense_shift) - 1;
    ulong k = (i / per) * p.sa_intv + ((i % per + 1) << p.dense_shift), value = 0;
    bool present = id < p.count && i < p.dense_entries;
    if (present && k <= p.seq_len) {
        ulong steps = locate_walk(bwt, k, p.sa_intv - 1, p);
        value = steps + sa[k >> p.sa_shift];
    }
    if (present) low[i] = uint(value);
    uint bits = simd_or(uint(value >> 32) << (i & 31));
    if (lane == 0 && i < p.dense_entries) high[i >> 5] = bits;
}
