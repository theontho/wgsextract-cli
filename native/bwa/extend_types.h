#ifndef __METAL_VERSION__
#pragma once
#include <cstdint>
using ExtendU32 = uint32_t;
using ExtendI32 = int32_t;
#else
using ExtendU32 = uint;
using ExtendI32 = int;
#endif

// Seed extension (ksw_extend2 as mem_chain2aln calls it) shared by host and kernel.
enum : ExtendU32 {
    // Each job uses 16 lanes of a SIMD-group, two query columns per lane, and
    // keeps the query and one row of H and E in threadgroup memory.
    extend_lanes = 16,
    extend_max_query = 256,
    // ksw_extend2 retries with twice the band (MAX_BAND_TRY in bwamem.c).
    extend_tries = 2
};

struct ExtendParameters {
    ExtendU32 count;
    ExtendI32 o_del, e_del, o_ins, e_ins, zdrop, w;
    // bwa_fill_scmat's matrix: match, mismatch, and any pair with an N.
    ExtendI32 match, mismatch, ambiguous, max;
    // pen_clip5 and pen_clip3: the left and right extensions' end bonus.
    ExtendI32 left_bonus, right_bonus;
};

// mem_chain2aln's extensions of one seed. Offsets index the batch's bases;
// a zero query length means mem_chain2aln skips that side. The right
// extension starts from the left one's final score (left_h0 if skipped).
struct ExtendJob {
    ExtendU32 left_query, left_target, left_qlen, left_tlen, left_h0;
    ExtendU32 right_query, right_target, right_qlen, right_tlen;
};

// ksw_extend2's return value and outputs.
struct ExtendCall {
    ExtendI32 score, qle, tle, gtle, gscore, max_off;
};

// calls[0, left_tries) are the left tries, calls[2, 2 + right_tries) the right ones.
struct ExtendResult {
    ExtendCall calls[2 * extend_tries];
    ExtendI32 right_h0, left_tries, right_tries, pad;
};
