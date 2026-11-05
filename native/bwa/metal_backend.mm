#include "metal_backend.h"
#include "seeding_engine.h"

#include <algorithm>
#include <atomic>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <system_error>
#include <thread>
#include <unistd.h>

namespace {
using namespace wgse::seeding;

constexpr size_t threadgroup_size = 64;
constexpr size_t locate_rows_per_dispatch = size_t(1) << 21;

bool enabled = false;
// Cleared by the first GPU failure; the remaining batches use the CPU.
bool usable = true;
bool inject_failure = false, inject_locate_failure = false;
// Resolve the seeds' suffix-array rows (bwt_sa) on the GPU too.
bool locate_enabled = true;
// Test policy: CPU threads wait for the GPU instead of mapping unprepared pairs.
bool all_on_gpu = false;
// Reads per GPU batch: smaller batches finish a chunk sooner, larger ones keep
// more of the GPU busy.
size_t gpu_batch_reads = 16384;
std::string execution_failure;
std::unique_ptr<GpuSeeds> gpu;
std::unique_ptr<GpuLocate> locator;
const uint32_t *shared_bwt = nullptr;
const uint64_t *shared_sa = nullptr;

// GPU results for one read: its SMEM intervals and the positions of the
// suffix-array rows mem_chain() will look up for them (if located).
struct Prepared {
    const Intervals *intervals;
    const uint64_t *rows, *positions;
    size_t count;
};

// The prepared reads of the pair this thread is mapping, if the GPU prepared it.
struct Current {
    const uint8_t *seq[2];
    const Prepared *prepared[2];
};
thread_local Current current;

// The rows of the read whose cached intervals this thread is chaining. Any
// unexpected row ends the cursor and that read's lookups use upstream bwt_sa().
struct Cursor {
    const uint64_t *rows = nullptr, *positions = nullptr;
    size_t next = 0, count = 0;
};
thread_local Cursor cursor;
// Reads whose GPU positions were not all consumed in order; expected to stay 0.
std::atomic<size_t> locate_misses{0};

std::mutex totals_mutex;
size_t gpu_reads = 0, cpu_reads = 0, fallback_reads = 0, wasted_reads = 0;
size_t gpu_batches = 0, located_rows = 0;
double gpu_seconds = 0, locate_seconds = 0, map_seconds = 0, cpu_wait_seconds = 0;

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
    if (!locate_enabled || shared_sa == bwt->sa) return;
    const size_t page = static_cast<size_t>(getpagesize());
    locator.reset();
    shared_sa = bwt->sa;
    // Without a shareable sampled suffix array only seeding uses the GPU.
    if (!GpuLocate::supported(*bwt) || reinterpret_cast<uintptr_t>(bwt->sa) % page) return;
    auto shared = std::make_unique<GpuLocate>(kernel(), locate_rows_per_dispatch);
    size_t bytes = (bwt->n_sa * sizeof(uint64_t) + page - 1) / page * page;
    shared->sa = [gpu->runtime.device() newBufferWithBytesNoCopy:bwt->sa length:bytes
        options:MTLResourceStorageModeShared deallocator:nil];
    if (!shared->sa) return;
    shared->configure(*bwt);
    locator = std::move(shared);
}

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

// GPU results for one batch of consecutive pairs.
struct Batch {
    Reads input;
    std::vector<Intervals> output;
    std::vector<uint64_t> rows, positions;
    std::vector<Prepared> prepared;
    // Published pairs not yet mapped; the last one frees the results, so large
    // chunks (-K) do not keep every batch until the chunk ends.
    std::atomic<long> pending{0};

    void release() {
        output = {};
        rows = {};
        positions = {};
        prepared = {};
    }
};

// Splits one chunk between the GPU and upstream's worker1 on n_threads CPU
// threads. The GPU prepares batches of pairs from the front; a CPU thread maps
// the next prepared pair if there is one, and otherwise an unprepared pair from
// the back with upstream's own seeding and locate. Output does not depend on
// which side prepares a pair or on timing.
class Scheduler {
public:
    Scheduler(const mem_opt_t *opt, const bwt_t *bwt, int n, bseq1_t *seqs,
        void (*worker)(void *, long, int), void *data)
        : opt_(opt), bwt_(bwt), seqs_(seqs), worker_(worker), data_(data),
          stride_((opt->flag & MEM_F_PE) ? 2 : 1), back_(n / stride_),
          prepared_(size_t(n / stride_) * stride_, nullptr), owner_(size_t(n / stride_)) {}

    void run() {
        auto start = Clock::now();
        std::vector<std::thread> threads;
        try {
            for (int tid = 0; tid < opt_->n_threads; ++tid)
                threads.emplace_back(&Scheduler::cpu, this, tid);
        } catch (const std::system_error &) {
            // Map with the threads that started; the caller covers a missing tid.
        }
        gpu_loop();
        if (threads.size() < size_t(opt_->n_threads)) cpu(static_cast<int>(threads.size()));
        for (auto &thread : threads) thread.join();
        map_seconds += seconds(start);
    }

private:
    const mem_opt_t *opt_;
    const bwt_t *bwt_;
    bseq1_t *seqs_;
    void (*worker_)(void *, long, int);
    void *data_;
    const int stride_;
    std::mutex mutex_;
    std::condition_variable changed_;
    // Pairs [0, front_) and [back_, n) are taken; [0, ready_) are prepared or
    // were given up by the GPU; the GPU is packing [ready_, claimed_) if packing_.
    long front_ = 0, back_, ready_ = 0, claimed_ = 0;
    bool packing_ = false, gpu_stopped_ = false;
    std::vector<const Prepared *> prepared_;
    // The batch of each pair in [0, ready_).
    std::vector<Batch *> owner_;
    std::vector<std::unique_ptr<Batch>> batches_;

    void cpu(int tid) {
        double waited = 0;
        size_t prepared = 0, missing = 0, unprepared = 0, wasted = 0;
        for (;;) {
            long pair;
            bool accelerated;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                auto wait = Clock::now();
                for (;;) {
                    if (front_ < std::min(ready_, back_)) {
                        pair = front_++;
                        accelerated = true;
                        break;
                    }
                    const bool steal = gpu_stopped_ || !all_on_gpu;
                    if (steal && back_ > (packing_ ? claimed_ : front_)) {
                        pair = --back_;
                        accelerated = false;
                        if (pair < claimed_) wasted += stride_;
                        break;
                    }
                    if (front_ >= back_) {
                        waited += seconds(wait);
                        std::lock_guard<std::mutex> totals(totals_mutex);
                        gpu_reads += prepared;
                        fallback_reads += missing;
                        cpu_reads += unprepared;
                        wasted_reads += wasted;
                        cpu_wait_seconds += waited;
                        return;
                    }
                    changed_.wait(lock);
                }
                waited += seconds(wait);
            }
            current = {};
            if (accelerated) {
                for (int mate = 0; mate < stride_; ++mate) {
                    const Prepared *entry = prepared_[size_t(pair) * stride_ + mate];
                    current.seq[mate] = entry ? reinterpret_cast<const uint8_t *>(
                        seqs_[pair * stride_ + mate].seq) : nullptr;
                    current.prepared[mate] = entry;
                    missing += !entry;
                }
                prepared += stride_;
            } else {
                unprepared += stride_;
            }
            worker_(data_, pair, tid);
            current = {};
            if (accelerated) {
                Batch *batch = owner_[size_t(pair)];
                if (batch->pending.fetch_sub(1, std::memory_order_acq_rel) == 1) batch->release();
            }
        }
    }

    void stop_gpu() {
        std::lock_guard<std::mutex> lock(mutex_);
        packing_ = false;
        gpu_stopped_ = true;
        changed_.notify_all();
    }

    void gpu_loop() {
        auto start = Clock::now();
        try {
            if (inject_failure) throw std::runtime_error("injected GPU failure (test)");
            const long pairs_per_batch = std::max<long>(1, long(gpu->capacity) / stride_);
            for (;;) {
                long first, last;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    if (ready_ >= back_) break;
                    first = ready_;
                    last = claimed_ = std::min(back_, first + pairs_per_batch);
                    packing_ = true;
                }
                auto batch = std::make_unique<Batch>();
                std::vector<long> slots;
                for (long pair = first; pair < last; ++pair) {
                    for (int mate = 0; mate < stride_; ++mate) {
                        if (pack(seqs_[pair * stride_ + mate], batch->input))
                            slots.push_back((pair - first) * stride_ + mate);
                    }
                }
                {
                    // CPU threads may now map this batch's pairs from the back.
                    std::lock_guard<std::mutex> lock(mutex_);
                    packing_ = false;
                    changed_.notify_all();
                }
                prepare(*batch, slots, first);
                batch->input = {};
                std::lock_guard<std::mutex> lock(mutex_);
                // Pairs stolen from the back are >= back_; after this no CPU
                // thread steals below ready_, so the front maps the rest.
                const long published = std::min(last, back_);
                if (published > first) {
                    batch->pending.store(published - first, std::memory_order_relaxed);
                    std::fill(owner_.begin() + first, owner_.begin() + published, batch.get());
                } else {
                    batch->release();
                }
                ready_ = last;
                ++gpu_batches;
                batches_.push_back(std::move(batch));
                changed_.notify_all();
            }
        } catch (const std::exception &error) {
            record_failure(error.what());
        }
        stop_gpu();
        gpu_seconds += seconds(start);
    }

    // Seeds the batch and locates the seeds' rows. Reads the GPU could not seed
    // keep a null entry and use the CPU.
    void prepare(Batch &batch, const std::vector<long> &slots, long first) {
        batch.output.resize(batch.input.reads.size());
        gpu->run(batch.input, batch.output, threadgroup_size, 0, bwt_, opt_);
        {
            // Skip locating a batch the CPU threads have already mapped.
            std::lock_guard<std::mutex> lock(mutex_);
            if (back_ <= first) return;
        }
        std::vector<char> usable_read(batch.output.size(), 1);
        for (long i : gpu->failed) usable_read[size_t(i)] = 0;
        std::vector<size_t> offsets(batch.output.size() + 1, 0);
        const bool locate = locator && opt_->max_occ > 0;
        if (locate) {
            auto phase = Clock::now();
            for (size_t i = 0; i < batch.output.size(); ++i) {
                if (usable_read[i]) append_rows(batch.output[i], opt_->max_occ, batch.rows);
                offsets[i + 1] = batch.rows.size();
            }
            if (inject_locate_failure)
                throw std::runtime_error("injected GPU locate failure (test)");
            locator->run(gpu->index, batch.rows, batch.positions, threadgroup_size);
            locate_seconds += seconds(phase);
            located_rows += batch.rows.size();
        }
        batch.prepared.resize(batch.output.size());
        for (size_t i = 0; i < batch.output.size(); ++i) {
            if (!usable_read[i]) continue;
            Prepared &entry = batch.prepared[i];
            entry.intervals = &batch.output[i];
            if (locate) {
                entry.rows = batch.rows.data() + offsets[i];
                entry.positions = batch.positions.data() + offsets[i];
                entry.count = offsets[i + 1] - offsets[i];
            }
            prepared_[size_t(first) * stride_ + size_t(slots[i])] = &entry;
        }
    }
};
} // namespace

extern "C" void *wgse_metal_calloc(size_t count, size_t size) {
    // Page-aligned so Metal can use the index in place; contents as calloc's.
    const size_t page = static_cast<size_t>(getpagesize());
    void *memory = nullptr;
    if ((size && count > (SIZE_MAX - page) / size)
        || posix_memalign(&memory, page, (count * size + page - 1) / page * page) != 0) {
        fprintf(stderr, "[wgse-bwa] failed to allocate %zu bytes for the index\n", count * size);
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
            Scheduler(opt, bwt, n, seqs, worker, data).run();
        } catch (const std::exception &error) {
            record_failure(error.what());
            return 0;
        }
    }
    return 1;
}

extern "C" int wgse_metal_take(const uint8_t *seq, bwtintv_v *out) {
    if (cursor.next < cursor.count) locate_misses.fetch_add(1, std::memory_order_relaxed);
    cursor = {};
    const Prepared *entry = current.seq[0] == seq ? current.prepared[0]
        : current.seq[1] == seq ? current.prepared[1] : nullptr;
    if (!entry) return 0;
    const auto &intervals = *entry->intervals;
    if (out->m < intervals.size()) {
        auto *storage = static_cast<bwtintv_t *>(
            realloc(out->a, intervals.size() * sizeof(bwtintv_t)));
        if (!storage) return 0;
        out->a = storage;
        out->m = intervals.size();
    }
    out->n = intervals.size();
    if (!intervals.empty()) memcpy(out->a, intervals.data(), intervals.size() * sizeof(bwtintv_t));
    cursor = {entry->rows, entry->positions, 0, entry->count};
    return 1;
}

extern "C" bwtint_t wgse_metal_sa(const bwt_t *bwt, bwtint_t k) {
    if (cursor.rows) {
        if (cursor.next < cursor.count && cursor.rows[cursor.next] == k)
            return cursor.positions[cursor.next++];
        locate_misses.fetch_add(1, std::memory_order_relaxed);
        cursor = {};
    }
    return bwt_sa(bwt, k);
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
    fprintf(stderr, "[wgse-bwa] gpu_batches=%zu gpu_reads=%zu cpu_reads=%zu fallback_reads=%zu "
        "wasted_reads=%zu located_rows=%zu locate_misses=%zu\n", gpu_batches, gpu_reads,
        cpu_reads, fallback_reads, wasted_reads, located_rows, locate_misses.load());
    fprintf(stderr, "[wgse-bwa] seconds gpu=%.3f locate=%.3f map=%.3f cpu_wait=%.3f\n",
        gpu_seconds, locate_seconds, map_seconds, cpu_wait_seconds);
    locator.reset();
    gpu.reset();
}

void wgse_set_gpu_policy(bool all) {
    all_on_gpu = all;
}

void wgse_set_gpu_batch(size_t reads) {
    gpu_batch_reads = reads;
}

void wgse_inject_gpu_failure() {
    inject_failure = true;
}

void wgse_inject_locate_failure() {
    inject_locate_failure = true;
}

void wgse_set_gpu_locate(bool enable) {
    locate_enabled = enable;
}
