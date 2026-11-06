// Seed extension: BWA 0.7.19's ksw_extend2 (banded global/local extension with
// Z-drop) and mem_chain2aln's band retries, for the first seed of each chain.
// Adapted from ksw.c (Attractive Chaos, MIT; see SEEDING-LICENSE). Each job
// uses 16 lanes of a SIMD-group. The query is split into T tiles of
// 16 * columns columns; lane l owns columns tile + l * columns + k, so H (as
// ksw_extend2's eh[j].h), E and the query stay in registers. Cells outside a
// row's band are not written and keep their stale values, exactly as
// ksw_extend2's eh[] array does. A row's F is a prefix maximum across lanes,
// which reproduces ksw_extend2's cell order. Each query length class has its
// own instantiation (see extend_class_caps), so registers fit the longest
// query of the class. extend_types.h is prepended by CMake.
#include <metal_stdlib>
using namespace metal;

constant constexpr int lanes = int(extend_lanes);

// One ksw_extend2 call on a query of at most T * 16 * columns bases. Every
// lane of the SIMD-group calls it (uniform control flow); lanes whose segment
// has no call pass run = false. Cells use 16-bit arithmetic: the host stages
// only calls whose scores, plus qlen * e_ins, fit in a short.
template <int T, int columns>
ExtendCall extend(bool run, int segment, int lane, const device uchar *query, int qlen,
    const device uchar *target, int tlen, int requested_w, int end_bonus, int h0,
    constant ExtendParameters &p)
{
    const ushort base_lane = ushort(segment * lanes);
    short H[T][columns], E[T][columns];
    uchar Q[T][columns];
    short JE[T][columns];
    for (int t = 0; t < T; ++t)
        for (int k = 0; k < columns; ++k) {
            const int j = lanes * columns * t + lane * columns + k;
            H[t][k] = short(j == 0 ? h0 : max(0, h0 - p.o_ins - p.e_ins * j));
            E[t][k] = 0;
            Q[t][k] = run && j < qlen ? query[j] : 0;
            JE[t][k] = short(j * p.e_ins);
        }
    // ksw_extend2 narrows the band to the longest gap that can still score.
    int w = min(requested_w, max(1, (qlen * p.max + end_bonus - p.o_ins) / p.e_ins + 1));
    w = min(w, max(1, (qlen * p.max + end_bonus - p.o_del) / p.e_del + 1));
    int best = h0, best_i = -1, best_j = -1, gscore = -1, gend = -1, max_off = 0;
    int beg = 0, end = qlen;
    const int oe_del = p.o_del + p.e_del;
    const short oe_del16 = short(oe_del), e_del = short(p.e_del), e_ins = short(p.e_ins), o_ins = short(p.o_ins);
    bool live = run;
    for (int i = 0;; ++i) {
        live = live && i < tlen;
        if (!simd_any(live)) break;
        short previous = 0, carry = 0, h1_init = 0;
        int row_best = 0, first = 1 << 20, last = -1, base = 0;
        if (live) {
            beg = max(beg, i - w);
            end = min(end, min(qlen, i + w + 1));
            h1_init = short(beg == 0 ? max(0, h0 - oe_del - p.e_del * i) : 0);
            previous = h1_init;
            base = int(target[i]);
        }
        for (int t = 0; t < T; ++t) {
            const int tile = lanes * columns * t;
            const bool on = live && tile <= end && tile + lanes * columns > beg;
            if (!simd_any(on)) continue;
            const int j0 = tile + lane * columns;
            short match[columns], prefix = 0;
            bool valid[columns];
            for (int k = 0; k < columns; ++k) {
                const int j = j0 + k;
                valid[k] = on && j >= beg && j < end;
                const short diagonal = H[t][k];
                const int q = int(Q[t][k]);
                const short score = short(base == 4 || q == 4 ? p.ambiguous : base == q ? p.match : p.mismatch);
                // H(i-1,j-1) == 0 starts no match (no cigar like 100M3I3D20M).
                match[k] = !valid[k] || diagonal == 0 ? short(0) : short(diagonal + score);
                if (valid[k]) prefix = max(prefix, short(match[k] + JE[t][k]));
            }
            // F(i,j) = max over j' < j of M(i,j') - o_ins - (j - j') * e_ins, floored at 0;
            // every M(i,j') + j' * e_ins is >= 0, so 0 is the scan's identity.
            for (ushort d = 1; d < lanes; d *= 2) {
                const short shifted = simd_shuffle_up(prefix, d);
                if (lane >= int(d)) prefix = max(prefix, shifted);
            }
            prefix = max(prefix, carry);
            short x = simd_shuffle_up(prefix, ushort(1));
            if (lane == 0) x = carry;
            short value[columns];
            for (int k = 0; k < columns; ++k) {
                const int j = j0 + k;
                value[k] = max(match[k], max(E[t][k], max(short(0), short(x - o_ins - JE[t][k]))));
                x = max(x, short(match[k] + JE[t][k]));
            }
            short last_value = value[0];
            for (int k = 1; k < columns; ++k)
                if (j0 + k < end) last_value = value[k];
            short preceding = simd_shuffle_up(last_value, ushort(1));
            if (lane == 0) preceding = previous;
            for (int k = 0; k < columns; ++k) {
                const int j = j0 + k;
                if (!on || j < beg || j > end) continue;
                // eh[j] = { H(i,j-1), E(i+1,j) }; eh[end] = { H(i,end-1), 0 }.
                const short before = j == beg ? h1_init : k == 0 ? preceding : value[k - 1];
                const short next_e = j == end ? short(0) : max(short(E[t][k] - e_del), max(short(0), short(match[k] - oe_del16)));
                H[t][k] = before;
                E[t][k] = next_e;
                if (j < end) {
                    if (before != 0 || next_e != 0) {
                        first = min(first, j);
                        last = max(last, j);
                    }
                    // The rightmost column wins ties, as ksw_extend2's mj does.
                    row_best = max(row_best, int(value[k]) * 512 + j + 1);
                }
            }
            const bool any = on && tile < end && tile + lanes * columns > beg;
            const short next_previous = simd_shuffle(last_value,
                ushort(base_lane + min(lanes - 1, max(0, (end - tile - 1) / columns))));
            const short next_carry = simd_shuffle(prefix, ushort(base_lane + lanes - 1));
            if (any) {
                previous = next_previous;
                carry = next_carry;
            }
        }
        for (ushort d = lanes / 2; d > 0; d /= 2) {
            row_best = max(row_best, simd_shuffle_xor(row_best, d));
            first = min(first, simd_shuffle_xor(first, d));
            last = max(last, simd_shuffle_xor(last, d));
        }
        if (live) {
            if (previous != 0) last = max(last, end);
            if (first == (1 << 20)) first = end;
            const int m = row_best >> 9, mj = (row_best & 511) - 1;
            if (max(beg, end) == qlen && int(previous) >= gscore) {
                gscore = previous;
                gend = i;
            }
            if (m == 0) {
                live = false;
            } else if (m > best) {
                best = m;
                best_i = i;
                best_j = mj;
                max_off = max(max_off, abs(mj - i));
            } else if (p.zdrop > 0) {
                const int drift = (i - best_i) - (mj - best_j);
                const int penalty = drift > 0 ? drift * p.e_del : -drift * p.e_ins;
                if (best - m - penalty > p.zdrop) live = false;
            }
            // The next row spans the columns whose H or E is nonzero.
            beg = first;
            end = min(qlen, max(last, beg - 1) + 2);
        }
    }
    return {best, best_j + 1, best_i + 1, gend + 1, gscore, max_off};
}

// The calls of one side of each job in `order`: the left side's tries, or the
// right side's, which start from the left side's final score. A batch runs
// the left side of all jobs with one, then the right side, each in order of
// decreasing query length so that the jobs sharing a SIMD-group take similar
// times.
template <int T, int C>
inline void extend_side(bool right, const device uchar *bases, const device ExtendJob *jobs,
    device ExtendResult *results, constant ExtendParameters &p, const device uint *order,
    uint thread_index, uint simd_lane)
{
    const int segment = int(simd_lane) / lanes, lane = int(simd_lane) % lanes;
    const uint slot = (thread_index / 32) * (32 / lanes) + uint(segment);
    const bool valid = slot < p.count;
    const uint index = valid ? order[slot] : 0;
    const ExtendJob job = valid ? jobs[index] : ExtendJob{0, 0, 0, 0, 1, 0, 0, 0, 0};
    device ExtendResult &r = results[index];
    int h0 = int(job.left_h0);
    if (right && valid && job.left_qlen > 0) h0 = r.calls[r.left_tries - 1].score;
    const uint query = right ? job.right_query : job.left_query;
    const uint target = right ? job.right_target : job.left_target;
    const int qlen = int(right ? job.right_qlen : job.left_qlen);
    const int tlen = int(right ? job.right_tlen : job.left_tlen);
    const int bonus = right ? p.right_bonus : p.left_bonus;
    // mem_chain2aln: retry with a doubled band unless the score is unchanged or
    // the alignment stayed well inside the band.
    int score = right ? h0 : -1, tries = 0;
    bool more = valid && qlen > 0;
    ExtendCall calls[extend_tries];
    for (int t = 0; t < int(extend_tries); ++t) {
        if (!simd_any(more)) break;
        const int w = p.w << t;
        const ExtendCall call = extend<T, C>(more, segment, lane, bases + query, qlen,
            bases + target, tlen, w, bonus, h0, p);
        if (more) {
            calls[t] = call;
            tries = t + 1;
            more = !(call.score == score || call.max_off < (w >> 1) + (w >> 2));
            score = call.score;
        }
    }
    if (!valid || lane != 0) return;
    const int first = right ? int(extend_tries) : 0;
    for (int t = 0; t < tries; ++t) r.calls[first + t] = calls[t];
    if (right) {
        r.right_h0 = h0;
        r.right_tries = tries;
    } else {
        r.left_tries = tries;
    }
}

#define EXTEND_SIDE(cap, T, C) \
kernel void extend_left_##cap(const device uchar *bases [[buffer(0)]], \
    const device ExtendJob *jobs [[buffer(1)]], device ExtendResult *results [[buffer(2)]], \
    constant ExtendParameters &p [[buffer(3)]], const device uint *order [[buffer(4)]], \
    uint thread_index [[thread_position_in_grid]], uint simd_lane [[thread_index_in_simdgroup]]) \
{ extend_side<T, C>(false, bases, jobs, results, p, order, thread_index, simd_lane); } \
kernel void extend_right_##cap(const device uchar *bases [[buffer(0)]], \
    const device ExtendJob *jobs [[buffer(1)]], device ExtendResult *results [[buffer(2)]], \
    constant ExtendParameters &p [[buffer(3)]], const device uint *order [[buffer(4)]], \
    uint thread_index [[thread_position_in_grid]], uint simd_lane [[thread_index_in_simdgroup]]) \
{ extend_side<T, C>(true, bases, jobs, results, p, order, thread_index, simd_lane); }

// Must match extend_class_caps: capacity = T * 16 * C.
EXTEND_SIDE(16, 1, 1)
EXTEND_SIDE(32, 1, 2)
EXTEND_SIDE(48, 1, 3)
EXTEND_SIDE(64, 1, 4)
EXTEND_SIDE(80, 1, 5)
EXTEND_SIDE(96, 1, 6)
EXTEND_SIDE(112, 1, 7)
EXTEND_SIDE(128, 1, 8)
EXTEND_SIDE(256, 4, 4)
