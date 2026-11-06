// Seed extension: BWA 0.7.19's ksw_extend2 (banded global/local extension with
// Z-drop) and mem_chain2aln's band retries, for the first seed of each chain.
// Adapted from ksw.c (Attractive Chaos, MIT; see SEEDING-LICENSE). Each job
// uses 16 lanes; a row's cells are scanned in tiles of 32 columns (two per
// lane) with F as a prefix maximum, which reproduces ksw_extend2's cell order.
// H and E persist across rows in threadgroup memory, so cells left outside
// the band keep their stale values exactly as ksw_extend2's eh[] array does.
// extend_types.h is prepended by CMake for runtime compilation.
#include <metal_stdlib>
using namespace metal;

constant constexpr int lanes = int(extend_lanes), columns = 2, max_query = int(extend_max_query);
constant constexpr int simds = 2;
constant constexpr int negative = -1000000000;

struct Row {
    threadgroup short *h, *e;
    threadgroup uchar *q;
};

// One ksw_extend2 call. Every lane of the SIMD-group calls it (uniform control
// flow); lanes whose segment has no call pass run = false.
ExtendCall extend(bool run, int segment, int lane, Row row, const device uchar *query, int qlen,
    const device uchar *target, int tlen, int requested_w, int end_bonus, int h0,
    constant ExtendParameters &p)
{
    const ushort base_lane = ushort(segment * lanes);
    if (run) {
        for (int j = lane; j <= qlen; j += lanes) {
            row.h[j] = short(j == 0 ? h0 : max(0, h0 - p.o_ins - p.e_ins * j));
            row.e[j] = 0;
            if (j < qlen) row.q[j] = query[j];
        }
    }
    simdgroup_barrier(mem_flags::mem_threadgroup);
    // ksw_extend2 narrows the band to the longest gap that can still score.
    int w = min(requested_w, max(1, (qlen * p.max + end_bonus - p.o_ins) / p.e_ins + 1));
    w = min(w, max(1, (qlen * p.max + end_bonus - p.o_del) / p.e_del + 1));
    int best = h0, best_i = -1, best_j = -1, gscore = -1, gend = -1, max_off = 0;
    int beg = 0, end = qlen;
    const int oe_del = p.o_del + p.e_del;
    bool live = run;
    for (int i = 0;; ++i) {
        live = live && i < tlen;
        if (!simd_any(live)) break;
        int tiles = 0, previous = 0, carry = negative, row_best = 0, first = 0, last = 0, base = 0;
        if (live) {
            beg = max(beg, i - w);
            end = min(end, min(qlen, i + w + 1));
            previous = beg == 0 ? max(0, h0 - oe_del - p.e_del * i) : 0;
            first = end;
            last = beg - 1;
            base = int(target[i]);
            tiles = (max(0, end - beg) + lanes * columns - 1) / (lanes * columns);
        }
        const int all_tiles = simd_max(tiles);
        for (int t = 0; t < all_tiles; ++t) {
            const bool on = t < tiles;
            const int tile = beg + t * lanes * columns, j0 = tile + lane * columns;
            int match[columns], del[columns], prefix = negative;
            for (int k = 0; k < columns; ++k) {
                const int j = j0 + k;
                const bool valid = on && j < end;
                const int diagonal = valid ? int(row.h[j]) : 0;
                del[k] = valid ? int(row.e[j]) : 0;
                const int q = valid ? int(row.q[j]) : 0;
                const int score = base == 4 || q == 4 ? p.ambiguous : base == q ? p.match : p.mismatch;
                // H(i-1,j-1) == 0 starts no match (no cigar like 100M3I3D20M).
                match[k] = !valid || diagonal == 0 ? 0 : diagonal + score;
                if (valid) prefix = max(prefix, match[k] + j * p.e_ins);
            }
            // F(i,j) = max over j' < j of M(i,j') - o_ins - (j - j') * e_ins, floored at 0.
            for (ushort d = 1; d < lanes; d *= 2) {
                const int shifted = simd_shuffle_up(prefix, d);
                if (lane >= int(d)) prefix = max(prefix, shifted);
            }
            prefix = max(prefix, carry);
            int x = simd_shuffle_up(prefix, 1);
            if (lane == 0) x = carry;
            int value[columns];
            for (int k = 0; k < columns; ++k) {
                const int j = j0 + k;
                value[k] = max(match[k], max(del[k], max(0, x - p.o_ins - j * p.e_ins)));
                x = max(x, match[k] + j * p.e_ins);
            }
            int last_value = value[0];
            for (int k = 1; k < columns; ++k)
                if (j0 + k < end) last_value = value[k];
            int preceding = simd_shuffle_up(last_value, 1);
            if (lane == 0) preceding = previous;
            if (on) {
                for (int k = 0; k < columns; ++k) {
                    const int j = j0 + k;
                    if (j >= end) continue;
                    // eh[j] = { H(i,j-1), E(i+1,j) }, as ksw_extend2 leaves it.
                    const int before = k == 0 ? preceding : value[k - 1];
                    const int next_e = max(del[k] - p.e_del, max(0, match[k] - oe_del));
                    row.h[j] = short(before);
                    row.e[j] = short(next_e);
                    if (before != 0 || next_e != 0) {
                        first = min(first, j);
                        last = max(last, j);
                    }
                    // The rightmost column wins ties, as ksw_extend2's mj does.
                    row_best = max(row_best, value[k] * 512 + j + 1);
                }
            }
            const int next_previous = simd_shuffle(last_value,
                ushort(base_lane + min(lanes - 1, max(0, (end - tile - 1) / columns))));
            const int next_carry = simd_shuffle(prefix, ushort(base_lane + lanes - 1));
            if (on) {
                previous = next_previous;
                carry = next_carry;
            }
        }
        if (live && lane == 0) {
            row.h[end] = short(previous);
            row.e[end] = 0;
            if (previous != 0) last = end;
        }
        for (ushort d = lanes / 2; d > 0; d /= 2) {
            row_best = max(row_best, simd_shuffle_xor(row_best, d));
            first = min(first, simd_shuffle_xor(first, d));
            last = max(last, simd_shuffle_xor(last, d));
        }
        if (live) {
            const int m = row_best >> 9, mj = (row_best & 511) - 1;
            if (max(beg, end) == qlen && previous >= gscore) {
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
        simdgroup_barrier(mem_flags::mem_threadgroup);
    }
    return {best, best_j + 1, best_i + 1, gend + 1, gscore, max_off};
}

// The calls of one side of each job in `order`: the left side's tries, or the
// right side's, which start from the left side's final score. A batch runs
// extend_left on all jobs with a left side, then extend_right, each in order
// of decreasing query length so that the jobs sharing a SIMD-group take
// similar times.
inline void extend_side(bool right, const device uchar *bases, const device ExtendJob *jobs,
    device ExtendResult *results, constant ExtendParameters &p, const device uint *order,
    uint thread_index, uint simd_lane, uint simd_group, Row row)
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
        const ExtendCall call = extend(more, segment, lane, row, bases + query, qlen,
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

#define EXTEND_SIDE(name, right) \
kernel void name(const device uchar *bases [[buffer(0)]], \
    const device ExtendJob *jobs [[buffer(1)]], device ExtendResult *results [[buffer(2)]], \
    constant ExtendParameters &p [[buffer(3)]], const device uint *order [[buffer(4)]], \
    uint thread_index [[thread_position_in_grid]], \
    uint simd_lane [[thread_index_in_simdgroup]], \
    uint simd_group [[simdgroup_index_in_threadgroup]]) \
{ \
    threadgroup short h[simds][32 / lanes][max_query + 1], e[simds][32 / lanes][max_query + 1]; \
    threadgroup uchar q[simds][32 / lanes][max_query]; \
    const int segment = int(simd_lane) / lanes; \
    const Row row{h[simd_group][segment], e[simd_group][segment], q[simd_group][segment]}; \
    extend_side(right, bases, jobs, results, p, order, thread_index, simd_lane, simd_group, row); \
}
EXTEND_SIDE(extend_left, false)
EXTEND_SIDE(extend_right, true)
