// BWA-MEM2 2.3's SMEM collection (mem_collect_smem's three passes), one read
// per thread. Adapted from FMI_search.cpp (Intel Corporation and Heng Li, MIT;
// see BWA-MEM2-LICENSE). seeding_types.h is prepended for runtime compilation.
#include <metal_stdlib>
using namespace metal;

// FMI_search's CP_OCC: occurrence counts at each 64-symbol checkpoint and the
// symbols after it as one-hot bit strings.
struct Checkpoint {
    long4 count;
    ulong4 one_hot;
};

struct Interval {
    long k, l, s;
    uint m, n;
};

// GET_OCC for all four bases: the checkpoint's counts plus the matches among
// its first p & 63 symbols.
long4 occurrences(device const Checkpoint *cp, long p) {
    const device Checkpoint &entry = cp[p >> 6];
    const uint y = uint(p & 63);
    const ulong mask = y ? ~0ul << (64 - y) : 0ul;
    return entry.count + long4(popcount(entry.one_hot & mask));
}

// FMI_search::backwardExt.
Interval backward(device const Checkpoint *cp, constant SeedParameters &p, Interval in, uint a) {
    const long4 sp = occurrences(cp, in.k), s = occurrences(cp, in.k + in.s) - sp;
    const long l3 = in.l + long(in.k <= p.sentinel && in.k + in.s > p.sentinel);
    const long l2 = l3 + s[3], l1 = l2 + s[2], l0 = l1 + s[1];
    Interval out = in;
    out.k = p.count[a] + sp[a];
    out.l = a == 0 ? l0 : a == 1 ? l1 : a == 2 ? l2 : l3;
    out.s = s[a];
    return out;
}

// Forward extension is backward extension with the BWT of the reverse complement.
Interval forward(device const Checkpoint *cp, constant SeedParameters &p, Interval in, uint a) {
    Interval swapped = in;
    swapped.k = in.l;
    swapped.l = in.k;
    const Interval extended = backward(cp, p, swapped, 3 - a);
    Interval out = extended;
    out.k = extended.l;
    out.l = extended.k;
    return out;
}

struct Output {
    device SeedSmem *data;
    uint capacity, size;

    bool push(Interval value) {
        if (size < capacity) data[size] = {value.m, value.n, value.k, value.l, value.s};
        return ++size <= capacity;
    }
};

struct Scratch {
    device Interval *data;
    uint stride, capacity;

    Interval get(uint i) const { return data[ulong(i) * stride]; }
    void set(uint i, Interval value) { data[ulong(i) * stride] = value; }
};

Interval initial(constant SeedParameters &p, uint a, uint x) {
    return {p.count[a], p.count[3 - a], p.count[a + 1] - p.count[a], x, x};
}

// FMI_search::getSMEMsOnePosOneThread for one read from x; returns next_x, or
// -1 when the output or scratch is full.
int one_position(device const Checkpoint *cp, constant SeedParameters &p,
    device const uchar *q, int length, int x, int min_intv, Scratch prev, thread Output &out) {
    int next_x = x + 1;
    uint a = q[x];
    if (a >= 4) return next_x;
    Interval smem = initial(p, a, uint(x));
    uint count = 0;
    int j;
    for (j = x + 1; j < length; ++j) {
        a = q[j];
        next_x = j + 1;
        if (a >= 4) break;
        Interval next = forward(cp, p, smem, a);
        next.n = uint(j);
        if (count >= prev.capacity) return -1;
        prev.set(count, smem);
        count += next.s != smem.s;
        if (next.s < min_intv) {
            next_x = j;
            break;
        }
        smem = next;
    }
    if (smem.s >= min_intv) {
        if (count >= prev.capacity) return -1;
        prev.set(count++, smem);
    }
    for (uint i = 0; i < count / 2; ++i) {
        Interval t = prev.get(i);
        prev.set(i, prev.get(count - i - 1));
        prev.set(count - i - 1, t);
    }
    for (j = x - 1; j >= 0; --j) {
        uint current = 0;
        // Upstream keeps the last kept size in an int.
        int last_s = -1;
        a = q[j];
        if (a > 3) break;
        uint i = 0;
        for (; i < count; ++i) {
            Interval smem = prev.get(i), next = backward(cp, p, smem, a);
            next.m = uint(j);
            if (next.s < min_intv && smem.n - smem.m + 1 >= uint(p.min_seed_len)) {
                if (!out.push(smem)) return -1;
                break;
            }
            if (next.s >= min_intv && next.s != long(last_s)) {
                last_s = int(next.s);
                prev.set(current++, next);
                break;
            }
        }
        for (++i; i < count; ++i) {
            Interval next = backward(cp, p, prev.get(i), a);
            next.m = uint(j);
            if (next.s >= min_intv && next.s != long(last_s)) {
                last_s = int(next.s);
                prev.set(current++, next);
            }
        }
        count = current;
        if (current == 0) break;
    }
    if (count != 0) {
        Interval smem = prev.get(0);
        if (smem.n - smem.m + 1 >= uint(p.min_seed_len) && !out.push(smem)) return -1;
    }
    return next_x;
}

kernel void collect_smems(device const Checkpoint *cp [[buffer(0)]],
    constant SeedParameters &p [[buffer(1)]],
    device const uchar *bases [[buffer(2)]],
    device const SeedRead *reads [[buffer(3)]],
    device Interval *scratch [[buffer(4)]],
    device SeedSmem *smems [[buffer(5)]],
    device SeedCount *counts [[buffer(6)]],
    uint id [[thread_position_in_grid]]) {
    if (id >= p.reads) return;
    const int length = int(reads[id].length);
    device const uchar *q = bases + reads[id].offset;
    Scratch prev = {scratch + id, p.reads, p.scratch};
    Output out = {smems + ulong(id) * p.capacity, p.capacity, 0};
    SeedCount result = {seed_overflow, {0, 0, 0}, 0};
    // Pass one: getSMEMsAllPosOneThread with min_intv 1.
    for (int x = 0; x < length;) {
        x = one_position(cp, p, q, length, x, 1, prev, out);
        if (x < 0) {
            counts[id] = result;
            return;
        }
    }
    result.pass[0] = out.size;
    // Pass two: re-seed from the middle of long, rare SMEMs.
    for (uint i = 0; i < result.pass[0]; ++i) {
        const SeedSmem smem = out.data[i];
        const int start = int(smem.m), end = int(smem.n) + 1;
        if (end - start < p.split_len || smem.s > p.split_width) continue;
        result.query_bases += uint(end - start);
        // Upstream's int32 min_intv.
        if (one_position(cp, p, q, length, (end + start) >> 1, int(smem.s + 1),
            prev, out) < 0) {
            counts[id] = result;
            return;
        }
    }
    result.pass[1] = out.size - result.pass[0];
    // Pass three: bwtSeedStrategyAllPosOneThread.
    if (p.third_pass) {
        const uint min_len = uint(p.min_seed_len + 1);
        for (int x = 0; x < length;) {
            int next_x = x + 1;
            uint a = q[x];
            if (a < 4) {
                Interval smem = initial(p, a, uint(x));
                for (int j = x + 1; j < length; ++j) {
                    next_x = j + 1;
                    a = q[j];
                    if (a >= 4) break;
                    smem = forward(cp, p, smem, a);
                    smem.n = uint(j);
                    if (smem.s < p.max_mem_intv && smem.n - smem.m + 1 >= min_len) {
                        if (smem.s > 0 && !out.push(smem)) {
                            counts[id] = result;
                            return;
                        }
                        break;
                    }
                }
            }
            x = next_x;
        }
    }
    result.pass[2] = out.size - result.pass[0] - result.pass[1];
    result.status = seed_ok;
    counts[id] = result;
}
