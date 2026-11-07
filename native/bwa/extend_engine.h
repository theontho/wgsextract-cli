#pragma once

#include "runtime.h"
#include "extend_types.h"

#include "bwamem.h"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>

namespace wgse::extend {
static_assert(sizeof(ExtendParameters) == 52 && sizeof(ExtendJob) == 36);
static_assert(sizeof(ExtendResult) == 112);
constexpr size_t threadgroup_size = 64;
// The longest query of each kernel class; extend.metal instantiates
// extend_left_<cap> and extend_right_<cap> for each.
constexpr uint32_t class_caps[] = {16, 32, 48, 64, 80, 96, 112, 128, extend_max_query};
constexpr size_t classes = sizeof(class_caps) / sizeof(class_caps[0]);
static_assert(class_caps[classes - 1] == extend_max_query);

// Both sides' pipelines for every class, or false if any fails to build.
inline bool pipelines(wgse::metal::Runtime &runtime, const char *source,
    __strong id<MTLComputePipelineState> (&out)[2][classes]) {
    id<MTLLibrary> library = runtime.library(source);
    if (!library) return false;
    for (int side = 0; side < 2; ++side)
        for (size_t k = 0; k < classes; ++k) {
            const std::string name = std::string(side ? "extend_right_" : "extend_left_")
                + std::to_string(class_caps[k]);
            out[side][k] = runtime.pipeline(library, name.c_str());
            if (!out[side][k] || threadgroup_size > out[side][k].maxTotalThreadsPerThreadgroup)
                return false;
        }
    return true;
}

// The kernel's parameters for opt's scoring, or false if ksw_extend2 would
// score differently than it models: a bwa_fill_scmat-style matrix with a
// positive maximum, positive gap extensions and sizes that keep its arithmetic
// in range.
inline bool parameters(const mem_opt_t &opt, ExtendParameters &p) {
    const int8_t *mat = opt.mat;
    p = {};
    p.match = mat[0];
    p.mismatch = mat[1];
    p.ambiguous = mat[4];
    int highest = 0;
    for (int i = 0; i < 5; ++i)
        for (int j = 0; j < 5; ++j) {
            const int value = mat[i * 5 + j];
            const int expected = i == 4 || j == 4 ? p.ambiguous : i == j ? p.match : p.mismatch;
            if (value != expected) return false;
            highest = std::max(highest, value);
        }
    p.max = highest;
    p.o_del = opt.o_del;
    p.e_del = opt.e_del;
    p.o_ins = opt.o_ins;
    p.e_ins = opt.e_ins;
    p.zdrop = opt.zdrop;
    p.w = opt.w;
    p.left_bonus = opt.pen_clip5;
    p.right_bonus = opt.pen_clip3;
    constexpr int limit = 1 << 16;
    const bool gaps = opt.o_del >= 0 && opt.o_ins >= 0 && opt.e_del > 0 && opt.e_ins > 0
        && opt.o_del < limit && opt.o_ins < limit && opt.e_del < limit && opt.e_ins < limit;
    const bool bounded = opt.w > 0 && opt.w < limit && std::abs(opt.zdrop) < limit
        && std::abs(opt.pen_clip5) < limit && std::abs(opt.pen_clip3) < limit;
    // Gap penalties must fit the kernel's 16-bit cells.
    const bool narrow = opt.o_del + opt.e_del <= INT16_MAX
        && opt.o_ins + int(extend_max_query) * opt.e_ins <= INT16_MAX;
    return highest > 0 && gaps && bounded && narrow;
}

// Job indices with a side to extend, longest query first (a counting sort),
// so that the jobs sharing a SIMD-group take similar times. Returns the count.
inline size_t order_side(const ExtendJob *jobs, size_t count, bool right, uint32_t *order) {
    size_t start[extend_max_query + 2] = {};
    auto length = [&](size_t i) { return right ? jobs[i].right_qlen : jobs[i].left_qlen; };
    for (size_t i = 0; i < count; ++i)
        if (length(i)) ++start[extend_max_query + 1 - length(i)];
    size_t total = 0;
    for (size_t k = 0; k < extend_max_query + 2; ++k) {
        const size_t n = start[k];
        start[k] = total;
        total += n;
    }
    for (size_t i = 0; i < count; ++i)
        if (length(i)) order[start[extend_max_query + 1 - length(i)]++] = uint32_t(i);
    return total;
}

// Runs batches of seed extensions on their own command queue, so they can
// overlap the seeding batches and each other.
class GpuExtend {
public:
    wgse::metal::Runtime &runtime;
    id<MTLComputePipelineState> kernels[2][classes];

    GpuExtend(wgse::metal::Runtime &metal, id<MTLComputePipelineState> const (&built)[2][classes])
        : runtime(metal) {
        if (!runtime.queue()) throw std::runtime_error("extension kernel unavailable");
        for (int side = 0; side < 2; ++side)
            for (size_t k = 0; k < classes; ++k) {
                kernels[side][k] = built[side][k];
                if (!kernels[side][k]) throw std::runtime_error("extension kernel unavailable");
            }
    }

    // Shared buffers for up to `count` jobs (their results and two orders) and `bytes` bases.
    void allocate(size_t bytes, size_t count, __strong id<MTLBuffer> &bases,
        __strong id<MTLBuffer> &jobs, __strong id<MTLBuffer> &results,
        __strong id<MTLBuffer> &order) {
        bases = runtime.allocate(bytes);
        jobs = runtime.allocate(count * sizeof(ExtendJob));
        results = runtime.allocate(count * sizeof(ExtendResult));
        order = runtime.allocate(2 * count * sizeof(uint32_t));
        if (!bases || !jobs || !results || !order)
            throw std::runtime_error("GPU extension allocation failed");
    }

    // Commits the left then the right extensions of jobs[0, count) without
    // waiting; `done` runs on completion with the command buffer, whose
    // results[i] are then mem_chain2aln's ksw_extend2 results for jobs[i].
    // `capacity` is the jobs the buffers were allocated for.
    void submit(const ExtendParameters &p, id<MTLBuffer> bases, id<MTLBuffer> jobs,
        id<MTLBuffer> results, id<MTLBuffer> order, size_t count, size_t capacity,
        void (^done)(id<MTLCommandBuffer>)) {
        @autoreleasepool {
            auto *indices = static_cast<uint32_t *>(order.contents);
            const auto *job = static_cast<const ExtendJob *>(jobs.contents);
            const size_t sides[2] = {order_side(job, count, false, indices),
                order_side(job, count, true, indices + capacity)};
            id<MTLCommandBuffer> command = [runtime.queue() commandBuffer];
            if (!command) throw std::runtime_error("Metal command allocation failed");
            for (int side = 0; side < 2; ++side) {
                if (sides[side] == 0) continue;
                // The jobs of each class are a contiguous run of the descending
                // order; the classes' dispatches are independent.
                id<MTLComputeCommandEncoder> encoder =
                    [command computeCommandEncoderWithDispatchType:MTLDispatchTypeConcurrent];
                if (!encoder) throw std::runtime_error("Metal command allocation failed");
                command.label = encoder.label = side ? @"extend_right" : @"extend_left";
                const uint32_t *slots = indices + side * capacity;
                auto length = [&](size_t slot) {
                    const ExtendJob &j = job[slots[slot]];
                    return side ? j.right_qlen : j.left_qlen;
                };
                size_t low = 0;
                for (size_t k = classes; k-- > 0 && low < sides[side];) {
                    const uint32_t below = k ? class_caps[k - 1] : 0;
                    size_t high = low;
                    while (high < sides[side] && length(high) > below) ++high;
                    if (high == low) continue;
                    ExtendParameters current = p;
                    current.count = static_cast<uint32_t>(high - low);
                    [encoder setComputePipelineState:kernels[side][k]];
                    [encoder setBuffer:bases offset:0 atIndex:0];
                    [encoder setBuffer:jobs offset:0 atIndex:1];
                    [encoder setBuffer:results offset:0 atIndex:2];
                    [encoder setBytes:&current length:sizeof(current) atIndex:3];
                    [encoder setBuffer:order offset:(side * capacity + low) * sizeof(uint32_t)
                               atIndex:4];
                    const size_t per_group = 32 / extend_lanes;
                    const size_t threads = (current.count + per_group - 1) / per_group * 32;
                    [encoder dispatchThreads:MTLSizeMake(threads, 1, 1)
                        threadsPerThreadgroup:MTLSizeMake(threadgroup_size, 1, 1)];
                    low = high;
                }
                [encoder endEncoding];
            }
            [command addCompletedHandler:done];
            wgse::metal::Runtime::commit(command);
        }
    }

    // A completed command buffer's GPU time, or a failure.
    static double finished(id<MTLCommandBuffer> command) {
        if (command.status != MTLCommandBufferStatusCompleted)
            throw std::runtime_error(std::string("GPU extension: ") + (command.error
                ? command.error.localizedDescription.UTF8String : "Metal command failed"));
        return std::max(0.0, command.GPUEndTime - command.GPUStartTime);
    }
};
} // namespace wgse::extend
