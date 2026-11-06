#pragma once

#include "runtime.h"
#include "rescue_types.h"

#include "bwamem.h"

#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace wgse::rescue {
static_assert(sizeof(RescueParameters) == 40 && sizeof(RescueJob) == 24);
static_assert(sizeof(RescueResult) == 32);

// The kernel's parameters for opt's scoring, or false if ksw_u8 would score
// differently than it models: a bwa_fill_scmat-style matrix (one match, one
// mismatch and one N score), a positive match score and byte-sized gaps.
inline bool parameters(const mem_opt_t &opt, RescueParameters &p) {
    const int8_t *mat = opt.mat;
    p = {};
    p.match = mat[0];
    p.mismatch = mat[1];
    p.ambiguous = mat[4];
    int lowest = 127, highest = 0;
    for (int i = 0; i < 5; ++i)
        for (int j = 0; j < 5; ++j) {
            const int value = mat[i * 5 + j];
            const int expected = i == 4 || j == 4 ? p.ambiguous : i == j ? p.match : p.mismatch;
            if (value != expected) return false;
            lowest = std::min(lowest, value);
            highest = std::max(highest, value);
        }
    // ksw_qinit's uint8_t shift and max.
    p.shift = -lowest;
    p.max = highest;
    p.o_del = opt.o_del;
    p.e_del = opt.e_del;
    p.o_ins = opt.o_ins;
    p.e_ins = opt.e_ins;
    const bool gaps = opt.o_del >= 0 && opt.e_del >= 0 && opt.o_ins >= 0 && opt.e_ins >= 0
        && opt.o_del + opt.e_del <= 255 && opt.o_ins + opt.e_ins <= 255;
    return lowest < 0 && highest > 0 && gaps;
}

// Runs batches of mate-rescue alignments; buffers grow to the largest batch.
class GpuRescue {
public:
    wgse::metal::Runtime &runtime;
    id<MTLComputePipelineState> pipeline;
    id<MTLBuffer> parameters = nil, bases = nil, jobs = nil, rows = nil, results = nil;
    double kernel_seconds = 0;

    GpuRescue(wgse::metal::Runtime &metal, id<MTLComputePipelineState> kernel)
        : runtime(metal), pipeline(kernel) {
        if (!pipeline) throw std::runtime_error("rescue kernel unavailable");
        parameters = runtime.allocate(sizeof(RescueParameters));
        if (!parameters) throw std::runtime_error("GPU rescue allocation failed");
    }

    // results[i] is ksw_align2's result for job i, unless its status is rescue_cpu.
    void run(const RescueParameters &p, const std::vector<uint8_t> &input,
        const std::vector<RescueJob> &work, size_t row_bytes, std::vector<RescueResult> &output,
        size_t group) {
        output.resize(work.size());
        if (work.empty()) return;
        @autoreleasepool {
            reserve(bases, input.size());
            reserve(jobs, work.size() * sizeof(RescueJob));
            reserve(rows, row_bytes);
            reserve(results, work.size() * sizeof(RescueResult));
            memcpy(bases.contents, input.data(), input.size());
            memcpy(jobs.contents, work.data(), work.size() * sizeof(RescueJob));
            RescueParameters current = p;
            current.count = static_cast<uint32_t>(work.size());
            memcpy(parameters.contents, &current, sizeof(current));
            // Each job uses one half (16 lanes) of a 32-wide SIMD-group.
            const size_t threads = (work.size() + 1) / 2 * 32;
            double elapsed = 0;
            if (!runtime.dispatch(pipeline, {bases, jobs, rows, results, parameters}, threads,
                group, elapsed)) throw std::runtime_error(runtime.failure());
            kernel_seconds += elapsed;
            memcpy(output.data(), results.contents, work.size() * sizeof(RescueResult));
        }
    }

private:
    void reserve(__strong id<MTLBuffer> &buffer, size_t bytes) {
        bytes = std::max<size_t>(bytes, 1);
        if (buffer && buffer.length >= bytes) return;
        buffer = nil;
        buffer = runtime.allocate(bytes + bytes / 4);
        if (!buffer) throw std::runtime_error("GPU rescue allocation failed");
    }
};
} // namespace wgse::rescue
