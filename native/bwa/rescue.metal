// Mate-rescue local alignment: BWA 0.7.19's ksw_align2 with KSW_XBYTE, i.e.
// ksw_u8 (Farrar's striped Smith-Waterman, 16 byte lanes) and its reverse pass.
// Adapted from ksw.c (Attractive Chaos, MIT; see SEEDING-LICENSE). The striping is
// reproduced exactly, not just the scores: ksw_u8 derives E from H before the
// lazy-F correction, so its results can depend on the 16-lane segment layout.
// rescue_types.h is prepended by CMake for runtime compilation.
#include <metal_stdlib>
using namespace metal;

constant constexpr int lanes = int(rescue_lanes), segments = int(rescue_max_segments);

int sub0(int a, int b) { return max(a - b, 0); }

// Maximum over the 16 lanes of this half of the SIMD-group.
int half_max(int value) {
    for (ushort offset = 8; offset > 0; offset >>= 1)
        value = max(value, simd_shuffle_xor(value, offset));
    return value;
}

struct Pass {
    int gmax, te, qe, rows;
};

// One ksw_u8 call. Every lane of the SIMD-group calls it (uniform control flow);
// lanes whose half has no job pass run = false. In the reverse pass the query is
// query[qe..0] and the target target[te..0] followed by target[te+1..tlen).
Pass striped(bool run, int lane, uint side, const device uchar *query, int qlen,
    int reverse_from, const device uchar *target, int tlen, int target_from,
    int endsc, device uchar *rows, constant RescueParameters &p)
{
    const int slen = (qlen + lanes - 1) / lanes;
    const int oe_del = p.o_del + p.e_del, oe_ins = p.o_ins + p.e_ins;
    // Loops over segments are unrolled to the constant bound so the arrays stay
    // in registers; segments at or beyond slen are inert.
    uchar Q[segments];
    ushort H[segments], E[segments], M[segments];
    _Pragma("unroll")
    for (int j = 0; j < segments; ++j) {
        const int position = lane * slen + j;
        Q[j] = !run || j >= slen || position >= qlen ? uchar(255)
            : query[reverse_from >= 0 ? reverse_from - position : position];
        H[j] = E[j] = M[j] = 0;
    }
    Pass out{0, -1, -1, 0};
    for (int i = 0; simd_any(run); ++i) {
        int base = 0, last = 0;
        if (run) {
            base = int(target[target_from >= i ? target_from - i : i]);
            _Pragma("unroll")
            for (int j = 0; j < segments; ++j)
                if (j == slen - 1) last = H[j];
        }
        // H(i-1, -1) for lane 0; the previous lane's last segment otherwise.
        int h = simd_shuffle_up(last, 1);
        if (lane == 0) h = 0;
        int f = 0, row_max = 0;
        if (run) {
            // The 8-bit profile: padding scores 0; N against anything is ambiguous.
            const int match = base == 4 ? p.ambiguous : p.match;
            const int mismatch = base == 4 ? p.ambiguous : p.mismatch;
            _Pragma("unroll")
            for (int j = 0; j < segments; ++j) {
                if (j < slen) {
                    const int q = int(Q[j]);
                    const int score = q == 255 ? 0 : q == 4 ? p.ambiguous
                        : base == q ? match : mismatch;
                    const int previous = H[j];
                    h = max(min(h + score + p.shift, 255) - p.shift, 0);
                    const int e = E[j];
                    h = max(h, max(e, f));
                    row_max = max(row_max, h);
                    H[j] = ushort(h);
                    E[j] = ushort(max(sub0(e, p.e_del), sub0(h, oe_del)));
                    f = max(sub0(f, p.e_ins), sub0(h, oe_ins));
                    h = previous;
                }
            }
        }
        // Lazy F: propagate F across lanes until no lane of the half can raise H.
        bool lazy = run;
        for (int k = 0; k < lanes; ++k) {
            const int shifted = simd_shuffle_up(f, 1);
            f = lane == 0 ? 0 : shifted;
            if (!simd_any(lazy)) break;
            _Pragma("unroll")
            for (int j = 0; j < segments; ++j) {
                const bool active = lazy && j < slen;
                bool raises = false;
                if (active) {
                    const int value = max(int(H[j]), f);
                    H[j] = ushort(value);
                    f = sub0(f, p.e_ins);
                    raises = f > sub0(value, oe_ins);
                }
                const ulong votes = ulong(simd_vote::vote_t(simd_ballot(raises)));
                if (active && ((votes >> (side * 16)) & 0xffff) == 0) lazy = false;
                if (!simd_any(lazy && j + 1 < slen)) break;
            }
        }
        const int imax = half_max(row_max);
        if (run) {
            if (rows && lane == 0) rows[i] = uchar(imax);
            out.rows = i + 1;
            if (imax > out.gmax) {
                out.gmax = imax;
                out.te = i;
                _Pragma("unroll")
                for (int j = 0; j < segments; ++j) M[j] = H[j];
                if (out.gmax + p.shift >= 255 || out.gmax >= endsc) run = false;
            }
            if (i + 1 >= tlen) run = false;
        }
    }
    // The query end: the leftmost position of the best row's maximum.
    int key = 0;
    _Pragma("unroll")
    for (int j = 0; j < segments; ++j)
        if (j < slen) key = max(key, int(M[j]) << 8 | (255 - (lane * slen + j)));
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
    uint thread_index [[thread_position_in_grid]],
    uint simd_lane [[thread_index_in_simdgroup]])
{
    const uint side = simd_lane / 16;
    const int lane = int(simd_lane % 16);
    const uint index = (thread_index / 32) * 2 + side;
    const bool valid = index < p.count;
    RescueJob job = valid ? jobs[index] : RescueJob{0, 0, 0, 0, 0, 0};
    const int xtra = int(job.xtra);
    const bool subo = (xtra & int(rescue_xsubo)) != 0;
    const int minsc = subo ? xtra & 0xffff : 0x10000;
    const int endsc = (xtra & int(rescue_xstop)) ? xtra & 0xffff : 0x10000;
    bool run = valid && job.tlen > 0;
    Pass forward = striped(run, lane, side, bases + job.query, int(job.qlen), -1,
        bases + job.target, int(job.tlen), -1, endsc,
        subo && valid ? rows + job.rows : nullptr, p);
    RescueResult r{0, -1, -1, -1, -1, -1, -1, rescue_ok};
    if (valid) {
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
    const bool start = valid && (xtra & int(rescue_xstart)) && !(subo && r.score < (xtra & 0xffff));
    // The reverse pass needs a forward hit; without one, leave it to the CPU.
    if (start && (r.qe < 0 || r.te < 0)) r.status = rescue_cpu;
    const bool reverse = start && r.status == rescue_ok;
    Pass back = striped(reverse, lane, side, bases + job.query, r.qe + 1, r.qe,
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
