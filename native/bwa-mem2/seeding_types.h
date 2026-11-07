#ifndef __METAL_VERSION__
#pragma once
#include <cstdint>
using SeedU32 = uint32_t;
using SeedI32 = int32_t;
using SeedI64 = int64_t;
#else
using SeedU32 = uint;
using SeedI32 = int;
using SeedI64 = long;
#endif

// BWA-MEM2's SMEM collection (mem_collect_smem) shared by host and kernel.
struct SeedParameters {
    // FMI_search's count[] (after load_index adds 1) and sentinel_index.
    SeedI64 count[5];
    SeedI64 sentinel;
    SeedU32 reads;
    // Scratch entries per read (the batch's longest read) and output slots per read.
    SeedU32 scratch, capacity;
    // opt->min_seed_len, mem_collect_smem's split_len, opt->split_width and
    // the int32 max_mem_intv it passes to the third pass, if that pass runs.
    SeedI32 min_seed_len, split_len, split_width, max_mem_intv;
    SeedU32 third_pass;
};

// SMEM without rid: the read is the output slot's.
struct SeedSmem {
    SeedU32 m, n;
    SeedI64 k, l, s;
};

// One read's 2-bit bases in the batch's base buffer.
struct SeedRead {
    SeedU32 offset, length;
};

enum : SeedU32 {
    seed_ok = 0,
    // More SMEMs than the read's output slots, or a full scratch: the CPU collects the read.
    seed_overflow = 1,
    // (Host) not sent to the GPU: too long, or with bytes upstream does not encode.
    seed_declined = 2
};

// SMEMs of each pass and the summed length of the pass-one SMEMs that pass two
// re-seeds (mem_collect_smem's mem_lim).
struct SeedCount {
    SeedU32 status, pass[3], query_bases;
};
