#ifndef __METAL_VERSION__
#pragma once
#endif

#ifdef __METAL_VERSION__
#include <metal_stdlib>
using namespace metal;
using SeedU64 = ulong;
using SeedU32 = uint;
using SeedI32 = int;
#else
#include <cstdint>
using SeedU64 = uint64_t;
using SeedU32 = uint32_t;
using SeedI32 = int32_t;
#endif

enum : SeedU32 {
    seed_scratch_capacity = 128,
    seed_output_capacity = 256,
    seed_max_length = 512
};

struct SeedInterval {
    SeedU64 x[3], info;
};

struct SeedRead {
    SeedU32 offset, length;
};

struct SeedParameters {
    SeedU64 primary, l2[5];
    SeedU32 count;
    SeedI32 min_seed_len, split_len, split_width, max_mem_intv;
    SeedU32 scratch_limit, output_limit, padding;
};

enum SeedStatus : SeedU32 {
    seed_ok = 0,
    seed_length_limit = 1,
    seed_scratch_overflow = 2,
    seed_output_overflow = 3
};

struct SeedResult {
    SeedU32 count, status;
};
