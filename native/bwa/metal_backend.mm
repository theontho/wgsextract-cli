#include "metal_backend.h"
#include "seeding_engine.h"

#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <future>
#include <memory>
#include <unordered_map>
#include <unistd.h>

namespace {
using namespace wgse::seeding;

constexpr size_t gpu_batch_reads = 32768;
constexpr size_t threadgroup_size = 64;

bool enabled = false;
// Cleared by the first GPU failure; the remaining batches use the CPU.
bool usable = true;
bool inject_failure = false;
// Pairs per hundred seeded on the GPU while the CPU maps the rest concurrently;
// 80 was fastest on hs38 (60, 90 and 100 were slower).
size_t gpu_percent = 80;
std::string execution_failure;
std::unique_ptr<GpuSeeds> gpu;
const uint32_t *shared_bwt = nullptr;
std::unordered_map<const uint8_t *, const Intervals *> cached;
size_t gpu_reads = 0, cpu_reads = 0, fallback_reads = 0, gpu_batches = 0;
double gpu_seconds = 0, cpu_overlap_seconds = 0, cpu_tail_seconds = 0;

SeedKernel &kernel() {
    static SeedKernel value;
    return value;
}

void record_failure(const std::string &reason) {
    usable = false;
    if (!execution_failure.empty()) return;
    execution_failure = reason;
    fprintf(stderr, "[wgse-bwa] CPU fallback: %s\n", reason.c_str());
}

void attach(const bwt_t *bwt, const mem_opt_t *opt) {
    if (!gpu) gpu = std::make_unique<GpuSeeds>(kernel(), gpu_batch_reads);
    if (shared_bwt != bwt->bwt) {
        const size_t page = static_cast<size_t>(getpagesize());
        if (reinterpret_cast<uintptr_t>(bwt->bwt) % page)
            throw std::runtime_error("Metal needs a disk-loaded index, not a shared-memory one");
        size_t bytes = (bwt->bwt_size * sizeof(uint32_t) + page - 1) / page * page;
        // The C index keeps ownership; no GPU command outlives mem_process_seqs.
        gpu->index = [gpu->runtime.device() newBufferWithBytesNoCopy:bwt->bwt length:bytes
            options:MTLResourceStorageModeShared deallocator:nil];
        if (!gpu->index) throw std::runtime_error("cannot share the BWT with Metal");
        shared_bwt = bwt->bwt;
    }
    gpu->configure(*bwt, *opt, seed_scratch_capacity, seed_output_capacity);
}

struct Mapping {
    void (*worker)(void *, long, int);
    void *data;
    std::vector<long> ids;
    static void work(void *opaque, long i, int tid) {
        auto &mapping = *static_cast<Mapping *>(opaque);
        mapping.worker(mapping.data, mapping.ids[i], tid);
    }
};

// Appends the 2-bit encoding mem_align1_core would produce, or returns false
// (and leaves the read to the CPU) if the GPU cannot reproduce it exactly.
bool pack(const bseq1_t &seq, Reads &input) {
    if (seq.l_seq < 1 || seq.l_seq > int(seed_max_length)
        || input.bases.size() + size_t(seq.l_seq) > UINT32_MAX) return false;
    const size_t offset = input.bases.size();
    for (int j = 0; j < seq.l_seq; ++j) {
        uint8_t base = static_cast<uint8_t>(seq.seq[j]);
        if (base > 127) {
            input.bases.resize(offset);
            return false;
        }
        input.bases.push_back(base < 4 ? base : nst_nt4_table[base]);
    }
    input.reads.push_back({static_cast<uint32_t>(offset), static_cast<uint32_t>(seq.l_seq)});
    return true;
}

// The GPU seeds the selected pairs while the upstream worker pool maps the rest.
// The pool then maps the GPU-seeded pairs, consuming their cached intervals;
// any read without them (overflow, unsupported or a failed batch) is seeded by
// upstream's own CPU code. Throws only before any read has been processed.
void map(const mem_opt_t *opt, const bwt_t *bwt, int n, bseq1_t *seqs,
    void (*worker)(void *, long, int), void *data) {
    Mapping cpu{worker, data, {}}, accelerated{worker, data, {}};
    Reads input;
    std::vector<const uint8_t *> pointers;
    const int stride = (opt->flag & MEM_F_PE) ? 2 : 1;
    size_t routed = 0;
    for (int i = 0; i < n / stride; ++i) {
        if (size_t(i) % 100 >= gpu_percent) {
            cpu.ids.push_back(i);
            continue;
        }
        accelerated.ids.push_back(i);
        for (int mate = 0; mate < stride; ++mate) {
            const auto &seq = seqs[i * stride + mate];
            ++routed;
            if (pack(seq, input)) pointers.push_back(reinterpret_cast<const uint8_t *>(seq.seq));
        }
    }
    std::vector<Intervals> output(input.reads.size());
    std::string failure;
    auto start = Clock::now();
    {
        auto cpu_work = std::async(std::launch::async, [&] {
            kt_for(opt->n_threads, Mapping::work, &cpu, static_cast<long>(cpu.ids.size()));
            return seconds(start);
        });
        try {
            if (inject_failure) throw std::runtime_error("injected GPU failure (test)");
            // Always run, even with no reads, so the previous chunk's failures are cleared.
            gpu->run(input, output, threadgroup_size, 0, bwt, opt);
        } catch (const std::exception &error) {
            failure = error.what();
        }
        gpu_seconds += seconds(start);
        cpu_overlap_seconds += cpu_work.get();
    }
    if (failure.empty()) {
        try {
            cached.reserve(pointers.size());
            for (size_t i = 0; i < pointers.size(); ++i) cached.emplace(pointers[i], &output[i]);
            for (long i : gpu->failed) cached.erase(pointers[i]);
        } catch (const std::exception &error) {
            cached.clear();
            failure = error.what();
        }
    }
    gpu_reads += routed;
    cpu_reads += cpu.ids.size() * stride;
    fallback_reads += routed - cached.size();
    if (failure.empty())
        gpu_batches += (input.reads.size() + gpu->capacity - 1) / gpu->capacity;
    else
        record_failure(failure);
    start = Clock::now();
    kt_for(opt->n_threads, Mapping::work, &accelerated,
        static_cast<long>(accelerated.ids.size()));
    cpu_tail_seconds += seconds(start);
    cached.clear();
}
} // namespace

extern "C" void *wgse_metal_calloc(size_t count, size_t size) {
    // Page-aligned so Metal can use the BWT in place; contents as calloc's.
    const size_t page = static_cast<size_t>(getpagesize());
    void *memory = nullptr;
    if ((size && count > (SIZE_MAX - page) / size)
        || posix_memalign(&memory, page, (count * size + page - 1) / page * page) != 0) {
        fprintf(stderr, "[wgse-bwa] failed to allocate %zu bytes for the BWT\n", count * size);
        exit(EXIT_FAILURE);
    }
    memset(memory, 0, (count * size + page - 1) / page * page);
    return memory;
}

extern "C" int wgse_metal_enabled(void) {
    return enabled;
}

extern "C" int wgse_metal_map(const mem_opt_t *opt, const bwt_t *bwt, int n,
    bseq1_t *seqs, void (*worker)(void *, long, int), void *data) {
    if (!enabled || !usable) return 0;
    // Upstream accepts wider or negative seeding parameters than the GPU's
    // 32-bit representation. Keep their CPU semantics instead of narrowing them.
    double split = double(opt->min_seed_len) * opt->split_factor + .499;
    if (opt->max_mem_intv > INT_MAX || opt->min_seed_len <= 0 || opt->split_width < 0
        || !std::isfinite(split) || split < 0 || split > INT_MAX) return 0;
    @autoreleasepool {
        try {
            attach(bwt, opt);
            map(opt, bwt, n, seqs, worker, data);
        } catch (const std::exception &error) {
            record_failure(error.what());
            return 0;
        }
    }
    return 1;
}

extern "C" int wgse_metal_take(const uint8_t *seq, bwtintv_v *out) {
    if (cached.empty()) return 0;
    const auto found = cached.find(seq);
    if (found == cached.end()) return 0;
    const auto &intervals = *found->second;
    if (out->m < intervals.size()) {
        auto *storage = static_cast<bwtintv_t *>(
            realloc(out->a, intervals.size() * sizeof(bwtintv_t)));
        if (!storage) return 0;
        out->a = storage;
        out->m = intervals.size();
    }
    out->n = intervals.size();
    if (!intervals.empty()) memcpy(out->a, intervals.data(), intervals.size() * sizeof(bwtintv_t));
    return 1;
}

bool wgse_metal_available(std::string &reason) {
    return kernel().available(reason);
}

bool wgse_metal_hardware_supported() {
    return kernel().hardware_supported();
}

void wgse_set_backend(Backend backend) {
    if (backend == Backend::cpu) return;
    std::string reason;
    if (wgse_metal_available(reason)) enabled = true;
    else fprintf(stderr, "[wgse-bwa] CPU fallback: %s\n", reason.c_str());
}

bool wgse_metal_execution_failed(std::string &reason) {
    reason = execution_failure;
    return !reason.empty();
}

void wgse_finish_backend() {
    fprintf(stderr, "[wgse-bwa] gpu_batches=%zu gpu_reads=%zu cpu_reads=%zu fallback_reads=%zu\n",
        gpu_batches, gpu_reads, cpu_reads, fallback_reads);
    fprintf(stderr, "[wgse-bwa] seconds gpu=%.3f cpu_overlap=%.3f cpu_tail=%.3f\n",
        gpu_seconds, cpu_overlap_seconds, cpu_tail_seconds);
    gpu.reset();
}

void wgse_set_gpu_percent(int percent) {
    gpu_percent = static_cast<size_t>(percent);
}

void wgse_inject_gpu_failure() {
    inject_failure = true;
}
