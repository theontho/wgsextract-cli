// Mate-rescue local alignment: BWA 0.7.19's ksw_align2 with KSW_XBYTE, i.e.
// ksw_u8 (Farrar's striped Smith-Waterman, 16 byte lanes) and its reverse pass.
// Adapted from ksw.c (Attractive Chaos, MIT; see SEEDING-LICENSE). The striping is
// reproduced exactly, not just the scores: ksw_u8 derives E from H before the
// lazy-F correction, so its results can depend on the 16-lane segment layout.
// rescue_types.h is prepended by CMake for runtime compilation.
#include <metal_stdlib>
using namespace metal;

constant constexpr int lanes = int(rescue_lanes), segments = int(rescue_max_segments);
// Every forward-pass query of a pipeline has this many segments (slen). The host
// groups jobs by it, so the segment loops below fold to its exact length.
constant int forward_segments [[function_constant(0)]];

int sub0(int a, int b) { return max(a - b, 0); }

// Maximum over the 16 lanes of this half of the SIMD-group.
int half_max(int value) {
    for (ushort offset = 8; offset > 0; offset >>= 1)
        value = max(value, simd_shuffle_xor(value, offset));
    return value;
}

// Whether segment j is part of a query with slen segments.
bool live(int j, int slen) { return j < forward_segments && j < slen; }

struct Pass {
    int gmax, te, qe, rows;
};

// One ksw_u8 call. Every lane of the SIMD-group calls it (uniform control flow);
// lanes whose half has no job pass run = false. In the reverse pass the query is
// query[qe..0] and the target target[te..0] followed by target[te+1..tlen).
template <bool forward>
Pass striped(bool run, int lane, uint side, const device uchar *query, int qlen,
    int reverse_from, const device uchar *target, int tlen, int target_from,
    int endsc, device uchar *rows, constant RescueParameters &p)
{
    // The reverse pass's query is a prefix of the forward one, so it has at most
    // forward_segments segments too.
    const int slen = forward ? forward_segments : (qlen + lanes - 1) / lanes;
    const int oe_del = p.o_del + p.e_del, oe_ins = p.o_ins + p.e_ins;
    // Loops over segments are unrolled so the arrays stay in registers; segments
    // at or beyond slen are inert.
    // Profile values (score + shift) of segment j against bases 0..3, a byte each.
    uint P[segments];
    ushort H[segments], E[segments], M[segments];
    const ushort shift = ushort(p.shift), e_del = ushort(p.e_del), e_ins = ushort(p.e_ins),
        oe_del16 = ushort(oe_del), oe_ins16 = ushort(oe_ins);
    // Profile values (score + shift) for a match, a mismatch and an N.
    const ushort match_s = ushort(p.match + p.shift), mismatch_s = ushort(p.mismatch + p.shift),
        ambiguous_s = ushort(p.ambiguous + p.shift);
    _Pragma("unroll")
    for (int j = 0; j < segments; ++j) {
        const int position = lane * slen + j;
        const uint q = !run || !live(j, slen) || position >= qlen ? 255u
            : uint(query[reverse_from >= 0 ? reverse_from - position : position]);
        uint packed = 0;
        for (uint b = 0; b < 4; ++b) {
            const uint value = q == 255 ? uint(shift) : q == 4 ? uint(ambiguous_s)
                : b == q ? uint(match_s) : uint(mismatch_s);
            packed |= value << (8 * b);
        }
        P[j] = packed;
        H[j] = E[j] = M[j] = 0;
    }
    Pass out{0, -1, -1, 0};
    for (int i = 0; simd_any(run); ++i) {
        ushort base = 0, last = 0;
        if (run) {
            base = ushort(target[target_from >= i ? target_from - i : i]);
            _Pragma("unroll")
            for (int j = 0; j < segments; ++j)
                if (live(j, slen) && j == slen - 1) last = H[j];
        }
        ushort h = ushort(simd_shuffle_up(last, ushort(1)));
        if (lane == 0) h = 0;
        ushort f = 0, row_max = 0;
        if (run) {
            // An N in the target scores ambiguous against everything except padding.
            const uint lane_shift = base == 4 ? 0u : 8u * uint(base);
            // Segments below real hold query bases; the rest are padding.
            const int real = qlen - lane * slen;
            _Pragma("unroll")
            for (int j = 0; j < segments; ++j) {
                if (live(j, slen)) {
                    const ushort profile = ushort((P[j] >> lane_shift) & 255u);
                    const ushort score = base != 4 ? profile : j < real ? ambiguous_s : shift;
                    const ushort previous = H[j];
                    h = subsat(min(ushort(h + score), ushort(255)), shift);
                    const ushort e = E[j];
                    h = max(h, max(e, f));
                    row_max = max(row_max, h);
                    H[j] = h;
                    E[j] = max(subsat(e, e_del), subsat(h, oe_del16));
                    f = max(subsat(f, e_ins), subsat(h, oe_ins16));
                    h = previous;
                }
            }
        }
        // Lazy F (ksw_u8's SWPS3 loop): in round k every lane takes the F its
        // left neighbour ended round k - 1 with and, segment by segment, raises
        // H to it and lowers it by e_ins. The loop stops at the first segment
        // where no lane of the half has F - e_ins above H - oe_ins. F here never
        // depends on H, so each round tests all its segments at once and then
        // applies the raises up to the stopping segment.
        ushort round_f = simd_shuffle_up(f, ushort(1));
        if (lane == 0) round_f = 0;
        bool lazy = run;
        for (int k = 0; k < lanes && simd_any(lazy); ++k) {
            {
                // Most rounds stop at the first segment; test it alone first.
                const ushort value = max(H[0], round_f);
                const bool r0 = lazy && subsat(round_f, e_ins) > subsat(value, oe_ins16);
                if (!simd_any(r0)) {
                    if (lazy) H[0] = value;
                    break;
                }
            }
            uint fails = 0;
            ushort g = round_f;
            _Pragma("unroll")
            for (int j = 0; j < segments; ++j) {
                if (live(j, slen)) {
                    const ushort value = max(H[j], g);
                    g = subsat(g, e_ins);
                    fails |= uint(g > subsat(value, oe_ins16)) << j;
                }
            }
            for (ushort offset = 8; offset > 0; offset >>= 1)
                fails |= simd_shuffle_xor(fails, offset);
            // The stopping segment, or slen if every segment had a raising lane.
            const int stop = min(int(ctz(~fails)), slen);
            if (lazy) {
                g = round_f;
                _Pragma("unroll")
                for (int j = 0; j < segments; ++j) {
                    if (live(j, slen)) {
                        if (j <= stop) H[j] = max(H[j], g);
                        g = subsat(g, e_ins);
                    }
                }
                if (stop < slen) lazy = false;
            }
            // Lane 0 takes 0; F falls by e_ins per segment of the round.
            const ushort carried = simd_shuffle_up(g, ushort(1));
            round_f = lane == 0 ? ushort(0) : carried;
        }
        const int imax = half_max(int(row_max));
        if (run) {
            if (rows && lane == 0) rows[i] = uchar(imax);
            out.rows = i + 1;
            if (imax > out.gmax) {
                out.gmax = imax;
                out.te = i;
                _Pragma("unroll")
                for (int j = 0; j < segments; ++j)
                    if (live(j, slen)) M[j] = H[j];
                if (out.gmax + p.shift >= 255 || out.gmax >= endsc) run = false;
            }
            if (i + 1 >= tlen) run = false;
        }
    }
    // The query end: the leftmost position of the best row's maximum.
    int key = 0;
    _Pragma("unroll")
    for (int j = 0; j < segments; ++j)
        if (live(j, slen)) key = max(key, int(M[j]) << 8 | (255 - (lane * slen + j)));
    key = half_max(key);
    out.qe = 255 - (key & 255);
    return out;
}

kernel void rescue_align(
    const device uchar *bases [[buffer(0)]],
    const device RescueJob *jobs [[buffer(1)]],
    device uchar *rows [[buffer(2)]],
    device RescueResult *results [[buffer(3)]],
    constant RescueParameters &p [[buffer(4)]],
    const device uint *order [[buffer(5)]],
    uint thread_index [[thread_position_in_grid]],
    uint simd_lane [[thread_index_in_simdgroup]])
{
    const uint side = simd_lane / 16;
    const int lane = int(simd_lane % 16);
    // This dispatch's jobs are order[0, count), all with forward_segments segments.
    const uint slot = (thread_index / 32) * 2 + side;
    const bool valid = slot < p.count;
    const uint index = valid ? order[slot] : 0;
    RescueJob job = valid ? jobs[index] : RescueJob{0, 0, 0, 0, 0, 0};
    const int xtra = int(job.xtra);
    const bool subo = (xtra & int(rescue_xsubo)) != 0;
    const int minsc = subo ? xtra & 0xffff : 0x10000;
    const int endsc = (xtra & int(rescue_xstop)) ? xtra & 0xffff : 0x10000;
    RescueResult r{0, -1, -1, -1, -1, -1, -1, rescue_ok};
    // A job in the wrong group would be striped differently; leave it to the CPU.
    if (valid && (int(job.qlen) + lanes - 1) / lanes != forward_segments) r.status = rescue_cpu;
    bool run = valid && job.tlen > 0 && r.status == rescue_ok;
    Pass forward = striped<true>(run, lane, side, bases + job.query, int(job.qlen), -1,
        bases + job.target, int(job.tlen), -1, endsc,
        subo && valid ? rows + job.rows : nullptr, p);
    if (valid && r.status == rescue_ok) {
        r.score = forward.gmax + p.shift < 255 ? forward.gmax : 255;
        r.te = forward.te;
        if (r.score != 255) {
            r.qe = forward.qe;
            // Replays ksw_u8's b[] list of row maxima >= minsc, merging a row into
            // the previous entry while the maximum rises; then the best entry
            // outside the primary hit's diagonal window is the suboptimal score.
            if (subo && lane == 0) {
                const int width = (r.score + p.max - 1) / p.max;
                const int low = r.te - width, high = r.te + width;
                int entry_score = -1, entry_row = -1;
                bool have = false;
                for (int i = 0; i < forward.rows; ++i) {
                    const int value = int(rows[job.rows + i]);
                    if (value < minsc) continue;
                    if (!have || entry_row + 1 != i) {
                        if (have && (entry_row < low || entry_row > high) && entry_score > r.score2)
                            r.score2 = entry_score, r.te2 = entry_row;
                        entry_score = value;
                        entry_row = i;
                        have = true;
                    } else if (entry_score < value) {
                        entry_score = value;
                        entry_row = i;
                    }
                }
                if (have && (entry_row < low || entry_row > high) && entry_score > r.score2)
                    r.score2 = entry_score, r.te2 = entry_row;
            }
        }
    }
    const bool start = valid && r.status == rescue_ok && (xtra & int(rescue_xstart)) && !(subo && r.score < (xtra & 0xffff));
    // The reverse pass needs a forward hit; without one, leave it to the CPU.
    if (start && (r.qe < 0 || r.te < 0)) r.status = rescue_cpu;
    const bool reverse = start && r.status == rescue_ok;
    Pass back = striped<false>(reverse, lane, side, bases + job.query, r.qe + 1, r.qe,
        bases + job.target, int(job.tlen), r.te, r.score & 0xffff, nullptr, p);
    if (reverse) {
        const int score = back.gmax + p.shift < 255 ? back.gmax : 255;
        const int qe = score != 255 ? back.qe : -1;
        if (r.score == score) {
            r.tb = r.te - back.te;
            r.qb = r.qe - qe;
        }
    }
    if (valid && lane == 0) results[index] = r;
}
