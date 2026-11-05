#include <metal_stdlib>
using namespace metal;

struct Pair {
    int idr, idq, id, len1, len2, h0, seqid, regid;
    int score, tle, gtle, qle, gscore, max_off;
    int bandwidth;
};

struct Parameters {
    int count, bandwidth, open_del, extend_del, open_ins, extend_ins;
    int zdrop, end_bonus, max_substitution;
    char matrix[25];
    bool simple_matrix;
    uchar precision;
};

kernel void extend_batch(
    device Pair *pairs [[buffer(0)]],
    const device uchar *reference [[buffer(1)]],
    const device uchar *query [[buffer(2)]],
    constant Parameters &p [[buffer(3)]],
    uint thread_index [[thread_position_in_grid]],
    uint lane [[thread_index_in_simdgroup]],
    uint simd_index [[simdgroup_index_in_threadgroup]])
{
    uint index = thread_index / 32;
    if (index >= uint(p.count)) return;
    Pair pair = pairs[index];
    // Four independent SIMD groups; host dispatch uses 128 threads.
    // Scores fit ushort: h0 <= 32767 plus at most 256 * 127 matches.
    threadgroup ushort rows_h[4][257], rows_e[4][257];
    threadgroup uchar queries[4][256];
    threadgroup ushort *h = rows_h[simd_index], *e = rows_e[simd_index];
    threadgroup uchar *q = queries[simd_index];
    for (int j = int(lane); j <= pair.len2; j += 32) {
        h[j] = j == 0 ? pair.h0
            : max(0, pair.h0 - p.open_ins - p.extend_ins * j);
        e[j] = 0;
        if (j < pair.len2) q[j] = query[pair.idq + j];
    }
    simdgroup_barrier(mem_flags::mem_threadgroup);
    int width = min(pair.bandwidth, max(1,
        (pair.len2 * p.max_substitution + p.end_bonus - p.open_ins)
        / p.extend_ins + 1));
    width = min(width, max(1,
        (pair.len2 * p.max_substitution + p.end_bonus - p.open_del)
        / p.extend_del + 1));
    int best = pair.h0, best_i = -1, best_j = -1;
    int global_score = -1, global_end = -1, max_offset = 0;
    int begin = 0, end = pair.len2;
    int cells = 0;
    for (int i = 0; pair.len2 > 0 && i < pair.len1; ++i) {
        begin = max(begin, i - width);
        end = min(end, min(pair.len2, i + width + 1));
        if (p.precision != 0 && begin >= end) break;
        cells += max(0, end - begin);
        int previous = begin == 0
            ? max(0, pair.h0 - p.open_del - p.extend_del * (i + 1)) : 0;
        int prefix_carry = -1000000000;
        int local_best = 0;
        int first_active = end, last_active = begin - 1;
        int base = int(reference[pair.idr + i]);
        for (int tile = begin; tile < end; tile += 32) {
            int j = tile + int(lane);
            bool valid = j < end;
            int diagonal = valid ? h[j] : 0, deletion = valid ? e[j] : 0;
            int query_base = valid ? int(q[j]) : 0;
            int substitution = p.simple_matrix
                ? (base == 4 || query_base == 4 ? int(p.matrix[24])
                    : base == query_base ? int(p.matrix[0]) : int(p.matrix[1]))
                : int(p.matrix[base * 5 + query_base]);
            int match = !valid || diagonal == 0 ? 0 : diagonal + substitution;
            // F(j) is an exclusive prefix maximum of M(k) + k*gap_extend.
            int prefix = valid ? match + j * p.extend_ins : -1000000000;
            for (ushort distance = 1; distance < 32; distance *= 2) {
                int shifted = simd_shuffle_up(prefix, distance);
                if (lane >= distance) prefix = max(prefix, shifted);
            }
            prefix = max(prefix, prefix_carry);
            int exclusive = simd_shuffle_up(prefix, 1);
            if (lane == 0) exclusive = prefix_carry;
            int insertion = max(0, exclusive - p.open_ins - j * p.extend_ins);
            int value = max(match, max(deletion, insertion));
            int preceding = simd_shuffle_up(value, 1);
            if (lane == 0) preceding = previous;
            if (valid) {
                h[j] = preceding;
                e[j] = max(deletion - p.extend_del,
                    max(0, match - p.open_del - p.extend_del));
                if (preceding != 0 || e[j] != 0) {
                    first_active = min(first_active, j);
                    last_active = max(last_active, j);
                }
                // Nine endpoint bits preserve rightmost ties in one reduction.
                local_best = max(local_best, value * 512 + j + 1);
            }
            previous = simd_shuffle(value, uint(min(31, end - tile - 1)));
            prefix_carry = simd_shuffle(prefix, 31);
        }
        if (lane == 0) {
            h[end] = previous;
            e[end] = 0;
            if (previous != 0) last_active = end;
        }
        int packed_best = simd_max(local_best);
        int row_best = packed_best / 512, row_j = packed_best % 512 - 1;
        if (max(begin, end) == pair.len2 && previous >= global_score) {
            global_score = previous;
            global_end = i;
        }
        if (row_best == 0) break;
        if (row_best > best) {
            best = row_best;
            best_i = i;
            best_j = row_j;
            max_offset = max(max_offset, abs(row_j - i));
        } else if (p.zdrop > 0) {
            int drift = (i - best_i) - (row_j - best_j);
            int penalty = drift > 0
                ? drift * p.extend_del : -drift * p.extend_ins;
            if (best - row_best - penalty > p.zdrop) break;
        }
        begin = simd_min(first_active);
        end = min(pair.len2, max(simd_max(last_active), begin - 1) + 2);
        simdgroup_barrier(mem_flags::mem_threadgroup);
    }
    if (lane == 0) {
        pairs[index].score = best;
        pairs[index].qle = best_j + 1;
        pairs[index].tle = best_i + 1;
        pairs[index].gtle = global_end + 1;
        pairs[index].gscore = global_score;
        pairs[index].max_off = max_offset;
        pairs[index].regid = cells;
        pairs[index].idr = cells;
    }
}
