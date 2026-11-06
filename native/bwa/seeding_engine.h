#pragma once

#include "runtime.h"
#include "seed_oracle.h"
#include "seeding_source.h"
#include "seeding_types.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

extern "C" void kt_for(int, void (*)(void *, long, int), void *, long);

namespace wgse::seeding {
using Clock = std::chrono::steady_clock;
using Intervals = std::vector<bwtintv_t>;
static_assert(sizeof(SeedInterval) == sizeof(bwtintv_t));
static_assert(offsetof(SeedInterval, info) == offsetof(bwtintv_t, info));
static_assert(sizeof(SeedParameters) == 80 && sizeof(SeedRead) == 8);
static_assert(sizeof(LocateParameters) == 72);

inline double seconds(Clock::time_point start) {
    return std::chrono::duration<double>(Clock::now() - start).count();
}

struct Reads {
    std::vector<uint8_t> bases;
    std::vector<SeedRead> reads;
};

struct CpuJob {
    const bwt_t *bwt;
    const mem_opt_t *options;
    const Reads &reads;
    std::vector<Intervals> &out;
    std::vector<void *> scratch;

    CpuJob(const bwt_t *index, const mem_opt_t *opt, const Reads &input,
        std::vector<Intervals> &output, int threads)
        : bwt(index), options(opt), reads(input), out(output), scratch(threads) {
        for (void *&entry : scratch) entry = wgse_seed_scratch_create();
    }
    ~CpuJob() {
        for (void *entry : scratch) wgse_seed_scratch_destroy(entry);
    }
    static void work(void *opaque, long i, int tid) {
        auto &job = *static_cast<CpuJob *>(opaque);
        const auto &read = job.reads.reads[i];
        const bwtintv_v *intervals = wgse_seed_collect(job.options, job.bwt,
            static_cast<int>(read.length), job.reads.bases.data() + read.offset,
            job.scratch[tid]);
        if (intervals->n)
            job.out[i].assign(intervals->a, intervals->a + intervals->n);
        else
            job.out[i].clear();
    }
};

// Runtime-compiled seeding kernel. Compilation failure on supported hardware
// makes Metal unavailable; only a missing unified-memory device is unsupported.
struct SeedKernel {
    wgse::metal::Runtime runtime;
    id<MTLComputePipelineState> pipeline = nil, locate = nil, rescue = nil;

    SeedKernel() {
        if (runtime.device()) pipeline = runtime.compile(wgse_seeding_source, "collect_seeds");
        if (pipeline) locate = runtime.compile(wgse_seeding_source, "locate_rows");
        if (locate) rescue = runtime.compile(wgse_rescue_source, "rescue_align");
    }
    bool hardware_supported() const { return runtime.device() != nil; }
    bool available(std::string &reason) const {
        reason = pipeline && locate && rescue ? ""
            : runtime.failure().empty() ? "Metal kernels unavailable" : runtime.failure();
        return reason.empty();
    }
};

class GpuSeeds {
public:
    wgse::metal::Runtime &runtime;
    id<MTLComputePipelineState> pipeline;
    id<MTLBuffer> index = nil, parameters = nil, sequences = nil, reads = nil;
    id<MTLBuffer> scratch = nil, output = nil, results = nil;
    size_t capacity;
    double kernel_seconds = 0, fallback_seconds = 0;
    double pack_seconds = 0, dispatch_seconds = 0, unpack_seconds = 0;
    size_t fallback = 0, intervals = 0;
    size_t reasons[4]{};
    std::vector<long> failed;

    GpuSeeds(SeedKernel &kernel, size_t batch)
        : runtime(kernel.runtime), pipeline(kernel.pipeline), capacity(batch) {
        if (!pipeline) throw std::runtime_error("seeding kernel unavailable");
        parameters = runtime.allocate(sizeof(SeedParameters));
        reads = runtime.allocate(batch * sizeof(SeedRead));
        scratch = runtime.allocate(batch * seed_scratch_capacity * 3 * sizeof(SeedInterval));
        output = runtime.allocate(batch * seed_output_capacity * sizeof(SeedInterval));
        results = runtime.allocate(batch * sizeof(SeedResult));
        if (!parameters || !reads || !scratch || !output || !results)
            throw std::runtime_error("GPU batch allocation failed");
    }

    void configure(const bwt_t &bwt, const mem_opt_t &opt,
        size_t scratch_limit, size_t output_limit) {
        SeedParameters p{};
        p.primary = bwt.primary;
        std::copy_n(bwt.L2, 5, p.l2);
        p.min_seed_len = opt.min_seed_len;
        p.split_len = static_cast<int>(opt.min_seed_len * opt.split_factor + .499);
        p.split_width = opt.split_width;
        p.max_mem_intv = static_cast<int>(opt.max_mem_intv);
        p.scratch_limit = static_cast<uint32_t>(scratch_limit);
        p.output_limit = static_cast<uint32_t>(output_limit);
        memcpy(parameters.contents, &p, sizeof(p));
    }

    void load_index(bwt_t &bwt, const mem_opt_t &opt, size_t scratch_limit, size_t output_limit) {
        index = runtime.allocate_pages(bwt.bwt_size * sizeof(uint32_t));
        if (!index) throw std::runtime_error("page-aligned BWT allocation failed");
        memcpy(index.contents, bwt.bwt, bwt.bwt_size * sizeof(uint32_t));
        // Both processors subsequently traverse exactly the same physical BWT.
        free(bwt.bwt);
        bwt.bwt = static_cast<uint32_t *>(index.contents);
        configure(bwt, opt, scratch_limit, output_limit);
    }

    void run(const Reads &input, std::vector<Intervals> &out, size_t group, int fallback_threads,
        const bwt_t *bwt, const mem_opt_t *opt) {
        kernel_seconds = fallback_seconds = 0;
        pack_seconds = dispatch_seconds = unpack_seconds = 0;
        fallback = intervals = 0;
        std::fill_n(reasons, 4, 0);
        failed.clear();
        for (size_t offset = 0; offset < input.reads.size(); offset += capacity) {
            @autoreleasepool {
                auto phase = Clock::now();
                size_t count = std::min(capacity, input.reads.size() - offset);
                size_t start = input.reads[offset].offset;
                const auto &last = input.reads[offset + count - 1];
                size_t bytes = size_t(last.offset) + last.length - start;
                if (!sequences || sequences.length < std::max(size_t(1), bytes))
                    sequences = runtime.allocate(std::max(size_t(1), bytes));
                if (!sequences) throw std::runtime_error("GPU reads allocation failed");
                memcpy(sequences.contents, input.bases.data() + start, bytes);
                auto *descriptors = static_cast<SeedRead *>(reads.contents);
                for (size_t i = 0; i < count; ++i) {
                    descriptors[i] = input.reads[offset + i];
                    descriptors[i].offset -= start;
                }
                static_cast<SeedParameters *>(parameters.contents)->count =
                    static_cast<uint32_t>(count);
                pack_seconds += seconds(phase);
                phase = Clock::now();
                double elapsed = 0;
                if (!runtime.dispatch(pipeline,
                    {index, parameters, sequences, reads, scratch, output, results},
                    count, group, elapsed)) throw std::runtime_error(runtime.failure());
                kernel_seconds += elapsed;
                dispatch_seconds += seconds(phase);
                phase = Clock::now();
                const auto *counts = static_cast<const SeedResult *>(results.contents);
                const auto *seeds = static_cast<const SeedInterval *>(output.contents);
                for (size_t i = 0; i < count; ++i) {
                    auto &destination = out[offset + i];
                    if (counts[i].status > seed_output_overflow)
                        throw std::runtime_error("invalid GPU fallback status");
                    ++reasons[counts[i].status];
                    if (counts[i].status != seed_ok) {
                        ++fallback;
                        failed.push_back(static_cast<long>(offset + i));
                        destination.clear();
                    } else {
                        if (counts[i].count > seed_output_capacity)
                            throw std::runtime_error("invalid GPU interval count");
                        destination.resize(counts[i].count);
                        if (!destination.empty())
                            memcpy(destination.data(), seeds + i * seed_output_capacity,
                                destination.size() * sizeof(SeedInterval));
                    }
                }
                unpack_seconds += seconds(phase);
            }
        }
        // Zero workers leaves failed reads for the aligner's existing CPU pool.
        if (!failed.empty() && fallback_threads > 0) {
            auto start = Clock::now();
            CpuJob job(bwt, opt, input, out, fallback_threads);
            struct FallbackJob {
                CpuJob &job;
                const std::vector<long> &failed;
                static void work(void *opaque, long i, int tid) {
                    auto &state = *static_cast<FallbackJob *>(opaque);
                    CpuJob::work(&state.job, state.failed[i], tid);
                }
            } state{job, failed};
            kt_for(fallback_threads, FallbackJob::work, &state, static_cast<long>(failed.size()));
            fallback_seconds = seconds(start);
        }
        for (const auto &seeds : out) intervals += seeds.size();
    }
};
// The suffix-array rows mem_chain() passes to bwt_sa() for these intervals, in
// its order: at most max_occ evenly spaced rows per interval. The casts spell
// out C's implicit conversions so any wrap-around behaves identically.
// The number of rows append_rows() adds for these intervals.
inline size_t row_count(const Intervals &intervals, int max_occ) {
    size_t total = 0;
    for (const bwtintv_t &interval : intervals)
        total += interval.x[2] > static_cast<uint64_t>(max_occ) ? static_cast<size_t>(max_occ)
            : static_cast<size_t>(interval.x[2]);
    return total;
}

inline void append_rows(const Intervals &intervals, int max_occ, std::vector<uint64_t> &rows) {
    for (const bwtintv_t &interval : intervals) {
        const bwtintv_t *p = &interval;
        int step, count;
        int64_t k;
        const uint64_t limit = static_cast<uint64_t>(max_occ);
        step = p->x[2] > limit ? static_cast<int>(p->x[2] / limit) : 1;
        for (k = count = 0; static_cast<uint64_t>(k) < p->x[2] && count < max_occ;
            k += step, ++count)
            rows.push_back(p->x[0] + static_cast<uint64_t>(k));
    }
}

// Resolves suffix-array rows to reference positions (bwt_sa) on the GPU,
// sharing the BWT and the sampled suffix array with the CPU in place.
class GpuLocate {
public:
    wgse::metal::Runtime &runtime;
    id<MTLComputePipelineState> pipeline;
    id<MTLBuffer> parameters = nil, rows = nil, positions = nil, sa = nil;
    size_t capacity;
    double kernel_seconds = 0;

    GpuLocate(SeedKernel &kernel, size_t rows_per_dispatch)
        : runtime(kernel.runtime), pipeline(kernel.locate), capacity(rows_per_dispatch) {
        if (!pipeline) throw std::runtime_error("locate kernel unavailable");
        parameters = runtime.allocate(sizeof(LocateParameters));
        rows = runtime.allocate(capacity * sizeof(uint64_t));
        positions = runtime.allocate(capacity * sizeof(uint64_t));
        if (!parameters || !rows || !positions)
            throw std::runtime_error("GPU locate allocation failed");
    }

    // Returns false (CPU locate) for suffix-array samples the kernel cannot index.
    static bool supported(const bwt_t &bwt) {
        return bwt.sa && bwt.sa_intv > 0 && (bwt.sa_intv & (bwt.sa_intv - 1)) == 0
            && bwt.n_sa == (bwt.seq_len + bwt.sa_intv) / bwt.sa_intv;
    }

    void configure(const bwt_t &bwt) {
        LocateParameters p{};
        p.primary = bwt.primary;
        p.seq_len = bwt.seq_len;
        p.sa_intv = static_cast<uint64_t>(bwt.sa_intv);
        std::copy_n(bwt.L2, 5, p.l2);
        p.sa_shift = static_cast<uint32_t>(__builtin_ctzll(p.sa_intv));
        memcpy(parameters.contents, &p, sizeof(p));
    }

    void run(id<MTLBuffer> index, const std::vector<uint64_t> &input,
        std::vector<uint64_t> &output, size_t group) {
        kernel_seconds = 0;
        output.resize(input.size());
        for (size_t offset = 0; offset < input.size(); offset += capacity) {
            @autoreleasepool {
                size_t count = std::min(capacity, input.size() - offset);
                memcpy(rows.contents, input.data() + offset, count * sizeof(uint64_t));
                static_cast<LocateParameters *>(parameters.contents)->count =
                    static_cast<uint32_t>(count);
                double elapsed = 0;
                if (!runtime.dispatch(pipeline, {index, sa, parameters, rows, positions},
                    count, group, elapsed)) throw std::runtime_error(runtime.failure());
                kernel_seconds += elapsed;
                memcpy(output.data() + offset, positions.contents, count * sizeof(uint64_t));
            }
        }
    }
};
} // namespace wgse::seeding
