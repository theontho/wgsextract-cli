#include "metal_backend.h"
#include "rescue_engine.h"
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
using wgse::rescue::GpuRescue;

constexpr size_t threadgroup_size = 64;
constexpr size_t locate_rows_per_dispatch = size_t(1) << 21;
// Prepared batches whose pairs are not all mapped yet; bounds memory for large -K.
constexpr size_t max_outstanding_batches = 3;

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
// Rows one batch may locate on the GPU (bounds its host buffers at large -c);
// the remaining reads' rows use upstream bwt_sa().
size_t locate_rows_per_batch = locate_rows_per_dispatch;
// Mate rescue (mem_matesw's ksw_align2) on the GPU too.
bool rescue_enabled = true, inject_rescue_failure = false, verify_rescue = false;
size_t rescue_batch_pairs = 4096;
// Bounds one rescue batch's reads and reference windows (and its row scratch).
constexpr size_t rescue_batch_bytes = size_t(64) << 20;
std::string execution_failure;
std::unique_ptr<GpuSeeds> gpu;
std::unique_ptr<GpuLocate> locator;
std::unique_ptr<GpuRescue> rescuer;
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

void finish_cursor() {
    if (cursor.next < cursor.count) locate_misses.fetch_add(1, std::memory_order_relaxed);
    cursor = {};
}

std::mutex totals_mutex;
size_t gpu_reads = 0, cpu_reads = 0, fallback_reads = 0, wasted_reads = 0;
size_t gpu_batches = 0, located_rows = 0, unlocated_reads = 0;
double gpu_seconds = 0, locate_seconds = 0, map_seconds = 0, cpu_wait_seconds = 0;
size_t rescue_batches = 0, rescue_gpu_pairs = 0, rescue_cpu_pairs = 0, rescue_wasted_pairs = 0;
size_t rescue_jobs = 0, rescue_declined = 0, rescue_skipped = 0;
size_t rescue_hits = 0, rescue_misses = 0, rescue_mismatches = 0;
double rescue_gpu_seconds = 0, rescue_kernel_seconds = 0, rescue_seconds = 0,
    rescue_wait_seconds = 0;

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
bool pack_read(const bseq1_t &seq, Reads &input) {
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
struct BatchBase {
    // Published pairs not yet mapped; the last one frees the results, so large
    // chunks (-K) do not keep every batch until the chunk ends.
    std::atomic<long> pending{0};
    virtual ~BatchBase() = default;
    // Swapping (unlike assigning {}) also returns the vectors' capacity.
    virtual void release() = 0;
};

// Splits one chunk's pairs (single-end reads count as pairs) between the GPU
// and an upstream worker on n_threads CPU threads. The GPU prepares batches of
// pairs from the front; a CPU thread maps the next prepared pair if there is
// one, and otherwise an unprepared pair from the back with upstream's own code.
// Output does not depend on which side prepares a pair or on timing.
class Splitter {
public:
    // Totals for the metrics, in pairs, once run() returns.
    size_t prepared = 0, unprepared = 0, wasted = 0, batches = 0;
    double waited = 0, gpu_time = 0;

    Splitter(int threads, long pairs, long pairs_per_batch, const char *injected,
        void (*worker)(void *, long, int), void *data)
        : threads_(threads), per_batch_(std::max<long>(1, pairs_per_batch)), injected_(injected),
          worker_(worker), data_(data), back_(pairs), owner_(size_t(pairs)) {}
    virtual ~Splitter() = default;

    void run() {
        std::vector<std::thread> threads;
        try {
            threads.reserve(size_t(threads_));
            for (int tid = 0; tid < threads_; ++tid)
                threads.emplace_back(&Splitter::cpu, this, tid);
        } catch (const std::exception &) {
            // Map with the threads that started; this thread covers a missing tid.
        }
        // The GPU waits for workers to drain its batches, so it needs at least one.
        if (threads.empty()) stop_gpu();
        else gpu_loop();
        if (threads.size() < size_t(threads_)) cpu(static_cast<int>(threads.size()));
        for (auto &thread : threads) thread.join();
    }

protected:
    // Packs [first, last) for the GPU; CPU threads cannot take these pairs yet.
    virtual std::unique_ptr<BatchBase> pack(long first, long last) = 0;
    // The GPU work for a packed batch; may skip it if taken(first).
    virtual void prepare(BatchBase &batch, long first) = 0;
    // Around worker(pair) on its CPU thread; batch is null if the pair is unprepared.
    virtual void begin(long pair, BatchBase *batch) = 0;
    virtual void end() = 0;
    // On each CPU thread after its last pair.
    virtual void finish_thread() {}

    // Whether CPU threads have taken every pair of the batch that starts at first.
    bool taken(long first) {
        std::lock_guard<std::mutex> lock(mutex_);
        return back_ <= first;
    }

private:
    const int threads_;
    const long per_batch_;
    const char *injected_;
    void (*worker_)(void *, long, int);
    void *data_;
    std::mutex mutex_;
    std::condition_variable changed_, drained_;
    // Pairs [0, front_) and [back_, n) are taken; [0, ready_) are prepared or
    // were given up by the GPU; the GPU is packing [ready_, claimed_) if packing_.
    long front_ = 0, back_, ready_ = 0, claimed_ = 0;
    bool packing_ = false, gpu_stopped_ = false;
    size_t outstanding_ = 0;
    // The batch of each pair in [0, ready_).
    std::vector<BatchBase *> owner_;
    std::vector<std::unique_ptr<BatchBase>> batches_;

    void cpu(int tid) {
        double wait_time = 0;
        size_t accelerated_pairs = 0, unprepared_pairs = 0, wasted_pairs = 0;
        for (;;) {
            long pair;
            BatchBase *batch = nullptr;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                auto wait = Clock::now();
                for (;;) {
                    if (front_ < std::min(ready_, back_)) {
                        pair = front_++;
                        batch = owner_[size_t(pair)];
                        break;
                    }
                    const bool steal = gpu_stopped_ || !all_on_gpu;
                    if (steal && back_ > (packing_ ? claimed_ : front_)) {
                        pair = --back_;
                        if (pair < claimed_) ++wasted_pairs;
                        break;
                    }
                    if (front_ >= back_) {
                        wait_time += seconds(wait);
                        prepared += accelerated_pairs;
                        unprepared += unprepared_pairs;
                        wasted += wasted_pairs;
                        waited += wait_time;
                        lock.unlock();
                        finish_thread();
                        return;
                    }
                    changed_.wait(lock);
                }
                wait_time += seconds(wait);
            }
            if (batch) ++accelerated_pairs;
            else ++unprepared_pairs;
            begin(pair, batch);
            worker_(data_, pair, tid);
            end();
            if (batch && batch->pending.fetch_sub(1, std::memory_order_acq_rel) == 1) {
                batch->release();
                std::lock_guard<std::mutex> lock(mutex_);
                --outstanding_;
                drained_.notify_one();
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
            if (injected_) throw std::runtime_error(injected_);
            for (;;) {
                long first, last;
                {
                    std::unique_lock<std::mutex> lock(mutex_);
                    // Pairs of outstanding batches are taken before any steal, so
                    // their workers finish them and wake this loop.
                    drained_.wait(lock, [this] {
                        return ready_ >= back_ || outstanding_ < max_outstanding_batches;
                    });
                    if (ready_ >= back_) break;
                    first = ready_;
                    last = claimed_ = std::min(back_, first + per_batch_);
                    packing_ = true;
                }
                std::unique_ptr<BatchBase> batch = pack(first, last);
                {
                    // CPU threads may now map this batch's pairs from the back.
                    std::lock_guard<std::mutex> lock(mutex_);
                    packing_ = false;
                    changed_.notify_all();
                }
                prepare(*batch, first);
                std::lock_guard<std::mutex> lock(mutex_);
                // Pairs stolen from the back are >= back_; after this no CPU
                // thread steals below ready_, so the front maps the rest.
                const long published = std::min(last, back_);
                BatchBase *owner = batch.get();
                // If this throws, ready_ is unchanged and the CPU maps the pairs.
                batches_.push_back(std::move(batch));
                if (published > first) {
                    owner->pending.store(published - first, std::memory_order_relaxed);
                    std::fill(owner_.begin() + first, owner_.begin() + published, owner);
                    ++outstanding_;
                } else {
                    owner->release();
                }
                ready_ = last;
                ++batches;
                changed_.notify_all();
            }
        } catch (const std::exception &error) {
            record_failure(error.what());
        }
        stop_gpu();
        gpu_time += seconds(start);
    }
};

struct SeedBatch : BatchBase {
    Reads input;
    // The position (pair offset * stride + mate) of each packed read.
    std::vector<long> slots;
    std::vector<Intervals> output;
    std::vector<uint64_t> rows, positions;
    std::vector<Prepared> prepared;

    void release() override {
        std::vector<Intervals>().swap(output);
        std::vector<uint64_t>().swap(rows);
        std::vector<uint64_t>().swap(positions);
        std::vector<Prepared>().swap(prepared);
    }
};

inline int stride(const mem_opt_t *opt) {
    return (opt->flag & MEM_F_PE) ? 2 : 1;
}

// worker1 with GPU seeding and suffix-array locate.
class SeedScheduler : public Splitter {
public:
    std::atomic<size_t> missing{0};

    SeedScheduler(const mem_opt_t *opt, const bwt_t *bwt, int n, bseq1_t *seqs,
        void (*worker)(void *, long, int), void *data)
        : Splitter(opt->n_threads, n / stride(opt), long(gpu->capacity) / stride(opt),
              inject_failure ? "injected GPU failure (test)" : nullptr, worker, data),
          opt_(opt), bwt_(bwt), seqs_(seqs), stride_(stride(opt)),
          prepared_(size_t(n / stride_) * stride_, nullptr) {}

private:
    const mem_opt_t *opt_;
    const bwt_t *bwt_;
    bseq1_t *seqs_;
    const int stride_;
    std::vector<const Prepared *> prepared_;

    std::unique_ptr<BatchBase> pack(long first, long last) override {
        auto batch = std::make_unique<SeedBatch>();
        for (long pair = first; pair < last; ++pair)
            for (int mate = 0; mate < stride_; ++mate)
                if (pack_read(seqs_[pair * stride_ + mate], batch->input))
                    batch->slots.push_back((pair - first) * stride_ + mate);
        return batch;
    }

    void prepare(BatchBase &base, long first) override {
        auto &batch = static_cast<SeedBatch &>(base);
        locate(batch, first);
        // Only the GPU needs the packed reads and their slots.
        batch.input = {};
        std::vector<long>().swap(batch.slots);
    }

    void begin(long pair, BatchBase *batch) override {
        current = {};
        if (!batch) return;
        for (int mate = 0; mate < stride_; ++mate) {
            const Prepared *entry = prepared_[size_t(pair) * stride_ + mate];
            current.seq[mate] = entry ? reinterpret_cast<const uint8_t *>(
                seqs_[pair * stride_ + mate].seq) : nullptr;
            current.prepared[mate] = entry;
            if (!entry) missing.fetch_add(1, std::memory_order_relaxed);
        }
    }

    void end() override {
        // The pair's last read has no later wgse_metal_take() to check its rows.
        finish_cursor();
        current = {};
    }

    // Seeds the batch and locates the seeds' rows. Reads the GPU could not seed
    // keep a null entry and use the CPU.
    void locate(SeedBatch &batch, long first) {
        batch.output.resize(batch.input.reads.size());
        gpu->run(batch.input, batch.output, threadgroup_size, 0, bwt_, opt_);
        // Skip locating a batch the CPU threads have already mapped.
        if (taken(first)) return;
        std::vector<char> usable_read(batch.output.size(), 1);
        for (long i : gpu->failed) usable_read[size_t(i)] = 0;
        std::vector<size_t> offsets(batch.output.size() + 1, 0);
        std::vector<char> located(batch.output.size(), 0);
        const bool locate = locator && opt_->max_occ > 0;
        if (locate) {
            auto phase = Clock::now();
            size_t total = 0;
            for (size_t i = 0; i < batch.output.size(); ++i) {
                if (!usable_read[i]) continue;
                const size_t count = row_count(batch.output[i], opt_->max_occ);
                if (count <= locate_rows_per_batch - total) {
                    located[i] = 1;
                    total += count;
                } else {
                    ++unlocated_reads;
                }
            }
            batch.rows.reserve(total);
            for (size_t i = 0; i < batch.output.size(); ++i) {
                if (located[i]) append_rows(batch.output[i], opt_->max_occ, batch.rows);
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
            if (located[i]) {
                entry.rows = batch.rows.data() + offsets[i];
                entry.positions = batch.positions.data() + offsets[i];
                entry.count = offsets[i + 1] - offsets[i];
            }
            prepared_[size_t(first) * stride_ + size_t(batch.slots[i])] = &entry;
        }
    }
};

// mem_matesw()'s inputs for one alignment; equal keys mean equal ksw_align2 calls.
struct RescueKey {
    const uint8_t *ms;
    int64_t rb, re;
    int l_ms, is_rev, xtra;
    // Index into the batch's results, or skipped for windows left to the CPU.
    uint32_t result;
};
constexpr uint32_t skipped_job = UINT32_MAX;

struct RescueBatch : BatchBase {
    long first = 0;
    std::vector<uint8_t> bases;
    std::vector<RescueJob> jobs;
    size_t row_bytes = 0;
    std::vector<RescueKey> keys;
    std::vector<RescueResult> results;
    // The keys of pair first + i are [pair_keys[i], pair_keys[i + 1]).
    std::vector<uint32_t> pair_keys;

    void release() override {
        std::vector<uint8_t>().swap(bases);
        std::vector<RescueJob>().swap(jobs);
        std::vector<RescueKey>().swap(keys);
        std::vector<RescueResult>().swap(results);
        std::vector<uint32_t>().swap(pair_keys);
    }
};

// The GPU's alignments for the pair this thread is finishing.
struct RescueCurrent {
    const RescueBatch *batch = nullptr;
    uint32_t begin = 0, end = 0;
};
thread_local RescueCurrent rescue_current;
struct RescueTally {
    size_t hits = 0, misses = 0, mismatches = 0;
};
thread_local RescueTally rescue_tally;

// worker2 for paired-end chunks with GPU mate rescue: the GPU aligns every
// rescue window mem_sam_pe() may try for its pairs, and mem_matesw() uses the
// GPU's result for each window it actually tries.
class RescueScheduler : public Splitter {
public:
    size_t jobs = 0, declined = 0, skipped = 0;

    RescueScheduler(const mem_opt_t *opt, const bntseq_t *bns, const uint8_t *pac,
        const mem_pestat_t *pes, int n, bseq1_t *seqs, mem_alnreg_v *regs,
        const RescueParameters &parameters, void (*worker)(void *, long, int), void *data)
        : Splitter(opt->n_threads, n / 2, long(rescue_batch_pairs),
              inject_rescue_failure ? "injected GPU rescue failure (test)" : nullptr, worker,
              data),
          opt_(opt), bns_(bns), pac_(pac), pes_(pes), seqs_(seqs), regs_(regs),
          parameters_(parameters) {}

private:
    const mem_opt_t *opt_;
    const bntseq_t *bns_;
    const uint8_t *pac_;
    const mem_pestat_t *pes_;
    bseq1_t *seqs_;
    mem_alnreg_v *regs_;
    RescueParameters parameters_;

    static void emit(void *context, const uint8_t *ms, int l_ms, int is_rev, int64_t rb,
        int64_t re, const uint8_t *ref, int xtra) {
        auto &self = *static_cast<RescueScheduler *>(context);
        auto &batch = *self.packing_batch_;
        const int64_t tlen = re - rb;
        // ksw_i16 (scores of 250 or more) and long queries stay on the CPU, as
        // does whatever would exceed the batch's bounded buffers.
        if (!(xtra & KSW_XBYTE) || l_ms < 1 || l_ms > int(rescue_max_query) || tlen < 1
            || batch.bases.size() + size_t(l_ms) + size_t(tlen) > rescue_batch_bytes) {
            self.skip(batch, ms, rb, re, l_ms, is_rev, xtra);
            return;
        }
        const size_t query = batch.bases.size(), target = query + size_t(l_ms);
        batch.bases.resize(target + size_t(tlen));
        uint8_t *bases = batch.bases.data();
        bool valid = true;
        for (int i = 0; i < l_ms; ++i) {
            valid &= ms[i] <= 4;
            bases[is_rev ? query + size_t(l_ms - 1 - i) : query + size_t(i)] =
                is_rev ? (ms[i] < 4 ? 3 - ms[i] : 4) : ms[i];
        }
        for (int64_t i = 0; i < tlen; ++i) valid &= ref[i] <= 4;
        if (!valid) {
            batch.bases.resize(query);
            self.skip(batch, ms, rb, re, l_ms, is_rev, xtra);
            return;
        }
        memcpy(bases + target, ref, size_t(tlen));
        batch.jobs.push_back({uint32_t(query), uint32_t(target), uint32_t(batch.row_bytes),
            uint32_t(l_ms), uint32_t(tlen), uint32_t(xtra)});
        batch.row_bytes += size_t(tlen);
        batch.keys.push_back({ms, rb, re, l_ms, is_rev, xtra, uint32_t(batch.jobs.size() - 1)});
    }

    void skip(RescueBatch &batch, const uint8_t *ms, int64_t rb, int64_t re, int l_ms,
        int is_rev, int xtra) {
        ++skipped;
        batch.keys.push_back({ms, rb, re, l_ms, is_rev, xtra, skipped_job});
    }

    RescueBatch *packing_batch_ = nullptr;

    std::unique_ptr<BatchBase> pack(long first, long last) override {
        auto batch = std::make_unique<RescueBatch>();
        batch->first = first;
        batch->pair_keys.reserve(size_t(last - first) + 1);
        packing_batch_ = batch.get();
        for (long pair = first; pair < last; ++pair) {
            batch->pair_keys.push_back(uint32_t(batch->keys.size()));
            wgse_rescue_candidates(opt_, bns_, pac_, pes_, seqs_ + 2 * pair, regs_ + 2 * pair,
                emit, this);
        }
        batch->pair_keys.push_back(uint32_t(batch->keys.size()));
        packing_batch_ = nullptr;
        return batch;
    }

    void prepare(BatchBase &base, long first) override {
        auto &batch = static_cast<RescueBatch &>(base);
        if (!taken(first)) {
            rescuer->run(parameters_, batch.bases, batch.jobs, batch.row_bytes, batch.results,
                threadgroup_size);
            jobs += batch.jobs.size();
            for (const RescueResult &result : batch.results) {
                if (result.status != rescue_ok && result.status != rescue_cpu)
                    throw std::runtime_error("invalid GPU rescue status");
                declined += result.status == rescue_cpu;
            }
        }
        std::vector<uint8_t>().swap(batch.bases);
        std::vector<RescueJob>().swap(batch.jobs);
    }

    void begin(long pair, BatchBase *base) override {
        rescue_current = {};
        if (!base) return;
        const auto *batch = static_cast<const RescueBatch *>(base);
        const size_t index = size_t(pair - batch->first);
        rescue_current = {batch, batch->pair_keys[index], batch->pair_keys[index + 1]};
    }

    void end() override {
        rescue_current = {};
    }

    void finish_thread() override {
        std::lock_guard<std::mutex> lock(totals_mutex);
        rescue_hits += rescue_tally.hits;
        rescue_misses += rescue_tally.misses;
        rescue_mismatches += rescue_tally.mismatches;
        rescue_tally = {};
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
            auto start = Clock::now();
            SeedScheduler scheduler(opt, bwt, n, seqs, worker, data);
            scheduler.run();
            const size_t reads = size_t(stride(opt));
            std::lock_guard<std::mutex> lock(totals_mutex);
            gpu_reads += scheduler.prepared * reads;
            cpu_reads += scheduler.unprepared * reads;
            wasted_reads += scheduler.wasted * reads;
            fallback_reads += scheduler.missing.load();
            gpu_batches += scheduler.batches;
            gpu_seconds += scheduler.gpu_time;
            cpu_wait_seconds += scheduler.waited;
            map_seconds += seconds(start);
        } catch (const std::exception &error) {
            record_failure(error.what());
            return 0;
        }
    }
    return 1;
}

extern "C" int wgse_metal_pair(const mem_opt_t *opt, const bntseq_t *bns, const uint8_t *pac,
    const mem_pestat_t pes[4], int n, bseq1_t *seqs, mem_alnreg_v *regs,
    void (*worker)(void *, long, int), void *data) {
    RescueParameters parameters;
    if (!enabled || !usable || !rescue_enabled || !(opt->flag & MEM_F_PE)
        || (opt->flag & MEM_F_NO_RESCUE) || !wgse::rescue::parameters(*opt, parameters))
        return 0;
    @autoreleasepool {
        try {
            if (!rescuer) rescuer = std::make_unique<GpuRescue>(kernel().runtime, kernel().rescue);
            auto start = Clock::now();
            const double kernel_before = rescuer->kernel_seconds;
            RescueScheduler scheduler(opt, bns, pac, pes, n, seqs, regs, parameters, worker, data);
            scheduler.run();
            std::lock_guard<std::mutex> lock(totals_mutex);
            rescue_batches += scheduler.batches;
            rescue_gpu_pairs += scheduler.prepared;
            rescue_cpu_pairs += scheduler.unprepared;
            rescue_wasted_pairs += scheduler.wasted;
            rescue_jobs += scheduler.jobs;
            rescue_declined += scheduler.declined;
            rescue_skipped += scheduler.skipped;
            rescue_gpu_seconds += scheduler.gpu_time;
            rescue_kernel_seconds += rescuer->kernel_seconds - kernel_before;
            rescue_wait_seconds += scheduler.waited;
            rescue_seconds += seconds(start);
        } catch (const std::exception &error) {
            record_failure(error.what());
            return 0;
        }
    }
    return 1;
}

extern "C" int wgse_metal_matesw(const mem_opt_t *opt, const uint8_t *ms, uint8_t *seq, int l_ms,
    uint8_t *ref, int is_rev, int64_t rb, int64_t re, int xtra, kswr_t *aln) {
    const RescueCurrent &pair = rescue_current;
    if (!pair.batch) return 0;
    for (uint32_t i = pair.begin; i < pair.end; ++i) {
        const RescueKey &key = pair.batch->keys[i];
        if (key.ms != ms || key.rb != rb || key.re != re || key.l_ms != l_ms
            || key.is_rev != is_rev || key.xtra != xtra) continue;
        if (key.result == skipped_job) return 0;
        const RescueResult &r = pair.batch->results[key.result];
        if (r.status != rescue_ok) return 0;
        *aln = {r.score, r.te, r.qe, r.score2, r.te2, r.tb, r.qb};
        if (verify_rescue) {
            const kswr_t cpu = ksw_align2(l_ms, seq, int(re - rb), ref, 5, opt->mat, opt->o_del,
                opt->e_del, opt->o_ins, opt->e_ins, xtra, 0);
            if (memcmp(&cpu, aln, sizeof(cpu))) {
                if (rescue_tally.mismatches++ < 3)
                    fprintf(stderr, "[wgse-bwa] rescue mismatch l_ms=%d tlen=%lld xtra=%#x "
                        "gpu=%d,%d,%d,%d,%d,%d,%d cpu=%d,%d,%d,%d,%d,%d,%d\n", l_ms,
                        static_cast<long long>(re - rb), xtra, aln->score, aln->te, aln->qe,
                        aln->score2, aln->te2, aln->tb, aln->qb, cpu.score, cpu.te, cpu.qe,
                        cpu.score2, cpu.te2, cpu.tb, cpu.qb);
                *aln = cpu;
            }
        }
        ++rescue_tally.hits;
        return 1;
    }
    ++rescue_tally.misses;
    return 0;
}

extern "C" int wgse_metal_take(const uint8_t *seq, bwtintv_v *out) {
    finish_cursor();
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
        "wasted_reads=%zu unlocated_reads=%zu located_rows=%zu locate_misses=%zu\n", gpu_batches,
        gpu_reads, cpu_reads, fallback_reads, wasted_reads, unlocated_reads, located_rows,
        locate_misses.load());
    fprintf(stderr, "[wgse-bwa] seconds gpu=%.3f locate=%.3f map=%.3f cpu_wait=%.3f\n",
        gpu_seconds, locate_seconds, map_seconds, cpu_wait_seconds);
    fprintf(stderr, "[wgse-bwa] rescue_batches=%zu rescue_gpu_pairs=%zu rescue_cpu_pairs=%zu "
        "rescue_wasted_pairs=%zu rescue_jobs=%zu rescue_declined=%zu rescue_skipped=%zu "
        "rescue_hits=%zu rescue_misses=%zu rescue_mismatches=%zu\n", rescue_batches,
        rescue_gpu_pairs, rescue_cpu_pairs, rescue_wasted_pairs, rescue_jobs, rescue_declined,
        rescue_skipped, rescue_hits, rescue_misses, rescue_mismatches);
    fprintf(stderr, "[wgse-bwa] rescue seconds gpu=%.3f kernel=%.3f pair=%.3f cpu_wait=%.3f\n",
        rescue_gpu_seconds, rescue_kernel_seconds, rescue_seconds, rescue_wait_seconds);
    rescuer.reset();
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

void wgse_set_gpu_locate_rows(size_t rows) {
    locate_rows_per_batch = rows;
}

void wgse_set_gpu_locate(bool enable) {
    locate_enabled = enable;
}

void wgse_set_gpu_rescue(bool enable) {
    rescue_enabled = enable;
}

void wgse_set_gpu_rescue_batch(size_t pairs) {
    rescue_batch_pairs = pairs;
}

void wgse_inject_rescue_failure() {
    inject_rescue_failure = true;
}

void wgse_verify_gpu_rescue() {
    verify_rescue = true;
}
