#ifndef __METAL_VERSION__
#pragma once
#include <cstdint>
using RescueU32 = uint32_t;
using RescueI32 = int32_t;
#else
using RescueU32 = uint;
using RescueI32 = int;
#endif

// Mate-rescue Smith-Waterman (ksw_align2 with KSW_XBYTE) shared by host and kernel.
enum : RescueU32 {
    // ksw_u8 stripes the query over 16 byte lanes; the GPU keeps each lane's
    // segments in registers, so queries are limited to 16 segments.
    rescue_lanes = 16,
    rescue_max_segments = 16,
    rescue_max_query = rescue_lanes * rescue_max_segments,
    // ksw.h flags.
    rescue_xbyte = 0x10000,
    rescue_xstop = 0x20000,
    rescue_xsubo = 0x40000,
    rescue_xstart = 0x80000
};

struct RescueParameters {
    RescueU32 count;
    RescueI32 o_del, e_del, o_ins, e_ins;
    // bwa_fill_scmat's matrix: match, mismatch, and any pair with an N.
    RescueI32 match, mismatch, ambiguous;
    // ksw_qinit's 8-bit bias (-min score) and maximum score.
    RescueI32 shift, max;
};

// Offsets index the batch's base buffer (query, then target) and its row-maximum scratch.
struct RescueJob {
    RescueU32 query, target, rows, qlen, tlen, xtra;
};

enum : RescueI32 {
    rescue_ok = 0,
    // Inputs the kernel does not reproduce; the CPU runs ksw_align2 instead.
    rescue_cpu = 1
};

// kswr_t followed by a status.
struct RescueResult {
    RescueI32 score, te, qe, score2, te2, tb, qb, status;
};
