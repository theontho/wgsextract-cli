// BWA 0.7.19's bidirectional FM-index traversal and three-pass MEM collection.
// Adapted from bwt.c/bwamem.c (Heng Li et al., MIT; see SEEDING-LICENSE).
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

// Four independent 8-bit counts, as in upstream's cnt_table, without dependent
// table loads. Each block has only 128 symbols, so summation cannot carry.
uint seed_count4(uint word) {
    uint lo = word & 0x55555555u, hi = (word >> 1) & 0x55555555u;
    return popcount(~lo & ~hi & 0x55555555u)
        | (popcount(lo & ~hi) << 8)
        | (popcount(~lo & hi) << 16)
        | (popcount(lo & hi) << 24);
}

ulong4 seed_unpack(uint count) {
    return ulong4(count & 255, (count >> 8) & 255, (count >> 16) & 255, count >> 24);
}

ulong4 seed_occ4(device const uint *bwt, ulong k, constant SeedParameters &p) {
    if (k == ulong(-1)) return ulong4(0);
    k -= k >= p.primary;
    device const uint *block = bwt + (k >> 7) * 16;
    ulong4 checkpoint = *reinterpret_cast<device const ulong4 *>(block);
    uint end = uint(k & 127) >> 4, count = 0;
    for (uint i = 0; i < end; ++i) count += seed_count4(block[8 + i]);
    uint masked = uint(~k & 15);
    count += seed_count4(block[8 + end] & ~((1u << (masked * 2)) - 1)) - masked;
    return checkpoint + seed_unpack(count);
}

void seed_2occ4(device const uint *bwt, ulong k, ulong l,
    constant SeedParameters &p, thread ulong4 &left, thread ulong4 &right) {
    ulong a = k - (k >= p.primary), b = l - (l >= p.primary);
    if ((a >> 7) != (b >> 7) || k == ulong(-1) || l == ulong(-1)) {
        left = seed_occ4(bwt, k, p);
        right = seed_occ4(bwt, l, p);
        return;
    }
    device const uint *block = bwt + (a >> 7) * 16;
    ulong4 checkpoint = *reinterpret_cast<device const ulong4 *>(block);
    uint end_a = uint(a & 127) >> 4, end_b = uint(b & 127) >> 4;
    uint count = 0, i = 0;
    for (; i < end_a; ++i) count += seed_count4(block[8 + i]);
    uint masked = uint(~a & 15);
    uint partial = count
        + seed_count4(block[8 + i] & ~((1u << (masked * 2)) - 1)) - masked;
    left = checkpoint + seed_unpack(partial);
    for (; i < end_b; ++i) count += seed_count4(block[8 + i]);
    masked = uint(~b & 15);
    count += seed_count4(block[8 + i] & ~((1u << (masked * 2)) - 1)) - masked;
    right = checkpoint + seed_unpack(count);
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
