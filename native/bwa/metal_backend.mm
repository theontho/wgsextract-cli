#include "metal_backend.h"
#include "extend_engine.h"
#include "rescue_engine.h"
#include "seeding_engine.h"

#include <algorithm>
#include <atomic>
#include <climits>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <system_error>
#include <thread>
#include <sys/sysctl.h>
#include <unistd.h>

namespace {
using namespace wgse::seeding;
using wgse::rescue::GpuRescue;
using wgse::extend::GpuExtend;

constexpr size_t threadgroup_size = 64;
constexpr size_t locate_rows_per_dispatch = size_t(1) << 21;
// Prepared batches whose pairs are not all mapped yet; bounds memory for large -K.
constexpr size_t max_outstanding_batches = 3;

bool enabled = false;
// Cleared by the first GPU failure; the remaining batches use the CPU.
std::atomic<bool> usable{true};
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
// Derive suffix-array samples every 1 << shift rows (shift 4: about 0.8 GiB
// for hs38) once a run has mapped enough reads to repay their fixed GPU build
// (about 19 s, repaid after about 14M more reads); only on Macs with at least
// 16 GiB unless a test option sets the shift.
uint32_t locate_dense_shift = 4;
size_t locate_dense_after = 20000000;
// Short dispatches keep each command buffer well inside the GPU watchdog.
size_t locate_dense_slice = size_t(1) << 21;
bool locate_dense_forced = false;
constexpr uint64_t locate_dense_min_memory = uint64_t(16) << 30;
// Whether the build was attempted, and whether it succeeded.
bool dense_tried = false, dense_built = false;
double locate_dense_seconds = 0;
// Reads mapped by earlier chunks (the map stage runs one chunk at a time).
size_t mapped_reads = 0;
// The built samples, also used by the CPU's bwt_sa (wgse_metal_sa).
const uint32_t *dense_low = nullptr, *dense_high = nullptr;
// Mate rescue (mem_matesw's ksw_align2) on the GPU too.
bool rescue_enabled = true, inject_rescue_failure = false, verify_rescue = false;
size_t rescue_batch_pairs = 4096;
// Bounds one rescue batch's reads and reference windows (and its row scratch).
constexpr size_t rescue_batch_bytes = size_t(64) << 20;
// Seed extension (mem_chain2aln's ksw_extend2) on the GPU too.
bool extend_enabled = true, inject_extend_failure = false, verify_extend = false;
// Pairs per extension batch; all CPU threads fill the same batch.
size_t extend_batch_pairs = 1024;
// Extension batches (filling, queued, in flight or being finished). They bound
// the memory of extensions in flight; when none is free a thread extends on
// the CPU instead.
size_t extend_batch_count = 6;
// Batches running on the GPU at once.
constexpr size_t extend_in_flight = 2;
// A batch's jobs and bases per pair; a batch is full when it reaches either.
constexpr size_t extend_jobs_per_pair = 24, extend_bases_per_pair = 8192;
std::mutex failure_mutex;
std::string execution_failure;
std::unique_ptr<GpuSeeds> gpu;
std::unique_ptr<GpuLocate> locator;
std::unique_ptr<GpuRescue> rescuer;
std::unique_ptr<GpuExtend> extender;
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
size_t extend_batches = 0, extend_jobs = 0, extend_skipped = 0, extend_gpu_pairs = 0,
    extend_cpu_pairs = 0, extend_hits = 0, extend_retry_hits = 0, extend_misses = 0,
    extend_mismatches = 0;
double extend_gpu_seconds = 0, extend_kernel_seconds = 0;
// Chunks mapped while an earlier chunk was being paired (fastmap_stages.c.in).
size_t overlapped_chunks = 0;
std::atomic<size_t> pair_starts{0}, pairs_running{0};

SeedKernel &kernel() {
    static SeedKernel value;
    return value;
}

void record_failure(const std::string &reason) {
    usable = false;
    std::lock_guard<std::mutex> lock(failure_mutex);
    if (!execution_failure.empty()) return;
    execution_failure = reason;
    fprintf(stderr, "[wgse-bwa] CPU fallback: %s\n", reason.c_str());
}

// reads_before: reads of the earlier chunks.
void attach(const bwt_t *bwt, const mem_opt_t *opt, size_t reads_before) {
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
    if (locate_enabled && shared_sa != bwt->sa) {
        const size_t page = static_cast<size_t>(getpagesize());
        locator.reset();
        dense_low = dense_high = nullptr;
        dense_tried = dense_built = false;
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
    // The denser samples cost a fixed build, repaid only by long runs.
    if (!locator || dense_tried || !locate_dense_shift || reads_before < locate_dense_after)
        return;
    dense_tried = true;
    if (!locate_dense_forced) {
        uint64_t memory = 0;
        size_t size = sizeof(memory);
        if (sysctlbyname("hw.memsize", &memory, &size, nullptr, 0) != 0
            || memory < locate_dense_min_memory) return;
    }
    auto start = Clock::now();
    if (locator->densify(gpu->index, locate_dense_shift, locate_dense_slice, threadgroup_size)) {
        dense_low = static_cast<const uint32_t *>(locator->low.contents);
        dense_high = static_cast<const uint32_t *>(locator->high.contents);
        dense_built = true;
        locate_dense_seconds = seconds(start);
    }
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

    // With overlap, the next batch is packed while the GPU runs the previous
    // one; that pays only when packing is slow.
    Splitter(int threads, long pairs, long pairs_per_batch, bool overlap, const char *injected,
        void (*worker)(void *, long, int), void *data)
        : worker_(worker), data_(data), threads_(threads),
          per_batch_(std::max<long>(1, pairs_per_batch)), overlap_(overlap), injected_(injected),
          back_(pairs),
          owner_(size_t(pairs)) {}
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
    // Work other than mapping a pair, such as finishing pairs whose extensions
    // the GPU has run.
    struct Work {
        void *owner = nullptr;
        size_t index = 0;
    };

    void (*const worker_)(void *, long, int);
    void *const data_;
    std::mutex mutex_;
    std::condition_variable changed_;

    // Packs [first, last) for the GPU; CPU threads cannot take these pairs yet.
    virtual std::unique_ptr<BatchBase> pack(long first, long last) = 0;
    // The GPU work for a packed batch; may skip it if taken(first).
    virtual void prepare(BatchBase &batch, long first) = 0;
    // Around worker(pair) on its CPU thread; batch is null if the pair is unprepared.
    virtual void begin(long pair, BatchBase *batch) = 0;
    virtual void end() = 0;
    // On each CPU thread after its last pair.
    virtual void finish_thread() {}
    // Maps pair on CPU thread tid.
    virtual void map(long pair, BatchBase *batch, int tid) {
        begin(pair, batch);
        worker_(data_, pair, tid);
        end();
    }
    // With mutex_ held: takes other work, which CPU threads do before mapping pairs.
    virtual bool claim(Work &) { return false; }
    virtual void perform(const Work &, int) {}
    // With mutex_ held when tid has nothing to take: whether work it must wait
    // for is still in flight.
    virtual bool idle(int) { return false; }

    // Whether CPU threads have taken every pair of the batch that starts at first.
    bool taken(long first) {
        std::lock_guard<std::mutex> lock(mutex_);
        return back_ <= first;
    }

private:
    const int threads_;
    const long per_batch_;
    const bool overlap_;
    const char *injected_;
    std::condition_variable drained_;
    // Pairs [0, front_) and [back_, n) are taken; [0, ready_) are prepared or
    // were given up by the GPU; [ready_, next_) are packed or being prepared, and
    // the GPU is packing [.., claimed_) if packing_.
    long front_ = 0, back_, ready_ = 0, next_ = 0, claimed_ = 0;
    bool packing_ = false, gpu_stopped_ = false, gpu_stopping_ = false, packed_all_ = false;
    size_t outstanding_ = 0, unpublished_ = 0;
    // Packed batches waiting for the GPU, in pair order.
    struct Packed {
        std::unique_ptr<BatchBase> batch;
        long first, last;
    };
    std::deque<Packed> packed_;
    std::condition_variable packed_changed_;
    // The batch of each pair in [0, ready_).
    std::vector<BatchBase *> owner_;
    std::vector<std::unique_ptr<BatchBase>> batches_;

    void cpu(int tid) {
        double wait_time = 0;
        size_t accelerated_pairs = 0, unprepared_pairs = 0, wasted_pairs = 0;
        for (;;) {
            long pair = -1;
            BatchBase *batch = nullptr;
            Work work;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                auto wait = Clock::now();
                for (;;) {
                    if (claim(work)) break;
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
                    const bool busy = idle(tid);
                    if (front_ >= back_ && !busy) {
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
            if (pair < 0) {
                perform(work, tid);
                continue;
            }
            if (batch) ++accelerated_pairs;
            else ++unprepared_pairs;
            map(pair, batch, tid);
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

    // With overlap_, packs the next batch on a thread of its own (if one
    // starts) while the GPU runs the previous one; batches are published in pair
    // order either way.
    void gpu_loop() {
        auto start = Clock::now();
        try {
            if (injected_) throw std::runtime_error(injected_);
            std::thread packer;
            if (overlap_) {
                try {
                    packer = std::thread(&Splitter::pack_loop, this);
                } catch (const std::system_error &) {
                }
            }
            if (packer.joinable()) {
                prepare_loop();
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    gpu_stopping_ = true;
                }
                drained_.notify_all();
                packer.join();
            } else {
                for (;;) {
                    if (!pack_next()) break;
                    if (!prepare_next()) break;
                }
            }
        } catch (const std::exception &error) {
            record_failure(error.what());
        }
        stop_gpu();
        gpu_time += seconds(start);
    }

    void pack_loop() {
        try {
            while (pack_next()) {
            }
        } catch (const std::exception &error) {
            record_failure(error.what());
            std::lock_guard<std::mutex> lock(mutex_);
            gpu_stopping_ = true;
        }
        std::lock_guard<std::mutex> lock(mutex_);
        packing_ = false;
        packed_all_ = true;
        changed_.notify_all();
        packed_changed_.notify_all();
    }

    void prepare_loop() {
        try {
            while (prepare_next()) {
            }
        } catch (const std::exception &error) {
            record_failure(error.what());
        }
    }

    // Claims and packs the next batch; false when there is none to pack.
    bool pack_next() {
        long first, last;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            // Pairs of outstanding batches are taken before any steal, so
            // their workers finish them and wake this loop. Batches being
            // packed or prepared count toward the bound.
            drained_.wait(lock, [this] {
                return gpu_stopping_ || next_ >= back_
                    || (outstanding_ + unpublished_ < max_outstanding_batches
                        && unpublished_ < (overlap_ ? 2 : 1));
            });
            if (gpu_stopping_ || next_ >= back_) return false;
            first = next_;
            last = next_ = claimed_ = std::min(back_, first + per_batch_);
            packing_ = true;
            ++unpublished_;
        }
        std::unique_ptr<BatchBase> batch = pack(first, last);
        std::lock_guard<std::mutex> lock(mutex_);
        // CPU threads may now map this batch's pairs from the back.
        packing_ = false;
        changed_.notify_all();
        packed_.push_back({std::move(batch), first, last});
        packed_changed_.notify_one();
        return true;
    }

    // Prepares and publishes the next packed batch; false when there is none.
    bool prepare_next() {
        Packed next;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            packed_changed_.wait(lock, [this] {
                return !packed_.empty() || packed_all_ || gpu_stopping_;
            });
            if (packed_.empty() || gpu_stopping_) return false;
            next = std::move(packed_.front());
            packed_.pop_front();
        }
        prepare(*next.batch, next.first);
        std::lock_guard<std::mutex> lock(mutex_);
        // Pairs stolen from the back are >= back_; after this no CPU
        // thread steals below ready_, so the front maps the rest.
        const long published = std::min(next.last, back_);
        BatchBase *owner = next.batch.get();
        // If this throws, ready_ is unchanged and the CPU maps the pairs.
        batches_.push_back(std::move(next.batch));
        if (published > next.first) {
            owner->pending.store(published - next.first, std::memory_order_relaxed);
            std::fill(owner_.begin() + next.first, owner_.begin() + published, owner);
            ++outstanding_;
        } else {
            owner->release();
        }
        ready_ = next.last;
        --unpublished_;
        ++batches;
        changed_.notify_all();
        drained_.notify_all();
        return true;
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

// The seed extensions of one batch of pairs, in CPU/GPU-shared buffers of fixed size.
struct ExtendBatch {
    struct Pair {
        long pair;
        void *chains;
        // The pair's jobs.
        uint32_t first, last;
    };
    id<MTLBuffer> bases = nil, jobs = nil, results = nil, order = nil;
    // Capacities, and the space reserved by staged pairs.
    size_t base_capacity = 0, job_capacity = 0, base_count = 0, job_count = 0;
    std::vector<Pair> pairs;
    // Threads copying a staged pair's jobs in; a sealed batch takes no more
    // pairs and is queued for the GPU once its last writer is done.
    size_t writers = 0;
    bool sealed = false;
    // Pairs [0, next) are taken for finishing; `unfinished` of them are not finished.
    size_t next = 0, unfinished = 0;
    // Whether the GPU ran the jobs; if not, the pairs are finished on the CPU.
    bool extended = false;
    // Commits of the jobs (more than one after interruptions).
    int attempts = 0;

    void reset() {
        base_count = job_count = writers = next = unfinished = 0;
        attempts = 0;
        pairs.clear();
        sealed = extended = false;
    }
};
// Kept across chunks.
std::vector<std::unique_ptr<ExtendBatch>> extend_pool;

// The extensions of the pair a thread is chaining. Offsets index `bases`.
struct ExtendStage {
    std::vector<uint8_t> bases;
    std::vector<ExtendJob> jobs;
    int match = 0, max = 0, e_ins = 0;
    size_t skipped = 0;
    bool failed = false;
};

// Called by wgse_worker1_chain() with mem_chain2aln()'s first extension of a
// chain; stages it as a GPU job unless the kernel cannot reproduce it.
void stage_extension(void *context, int, const uint8_t *query, int l_query, int qbeg, int len,
    const uint8_t *rseq, int64_t seed, int64_t window) {
    auto &stage = *static_cast<ExtendStage *>(context);
    const int64_t left_qlen = qbeg, right_qlen = int64_t(l_query) - qbeg - len;
    const int64_t left_tlen = left_qlen ? seed : 0;
    const int64_t right_tlen = right_qlen ? window - seed - len : 0;
    const int64_t h0 = int64_t(len) * stage.match;
    // An exact match of the whole read has nothing to extend.
    if (left_qlen == 0 && right_qlen == 0) return;
    // The kernel's arithmetic is 16-bit: no cell exceeds h0 plus max per query
    // base, and the F scan adds up to qlen * e_ins to a cell.
    if (stage.failed || qbeg < 0 || len < 1 || left_qlen > int64_t(extend_max_query)
        || right_qlen < 0 || right_qlen > int64_t(extend_max_query) || left_tlen < 0
        || right_tlen < 0 || seed < 0 || h0 < 1
        || h0 + (left_qlen + right_qlen) * stage.max
                + std::max(left_qlen, right_qlen) * stage.e_ins > INT16_MAX
        || left_qlen + left_tlen + right_qlen + right_tlen > int64_t(extend_bases_per_pair)) {
        ++stage.skipped;
        return;
    }
    const size_t start = stage.bases.size();
    try {
        stage.bases.resize(start + size_t(left_qlen + left_tlen + right_qlen + right_tlen));
    } catch (const std::bad_alloc &) {
        stage.failed = true;
        return;
    }
    uint8_t *out = stage.bases.data() + start;
    bool valid = true;
    auto put = [&](uint8_t base) {
        valid &= base <= 4;
        *out++ = base;
    };
    ExtendJob job{};
    // mem_chain2aln reverses the left extension's query and target.
    job.left_query = uint32_t(start);
    for (int64_t i = 0; i < left_qlen; ++i) put(query[qbeg - 1 - i]);
    job.left_target = uint32_t(start + size_t(left_qlen));
    for (int64_t i = 0; i < left_tlen; ++i) put(rseq[seed - 1 - i]);
    job.right_query = uint32_t(start + size_t(left_qlen + left_tlen));
    for (int64_t i = 0; i < right_qlen; ++i) put(query[qbeg + len + i]);
    job.right_target = job.right_query + uint32_t(right_qlen);
    for (int64_t i = 0; i < right_tlen; ++i) put(rseq[seed + len + i]);
    job.left_qlen = uint32_t(left_qlen);
    job.left_tlen = uint32_t(left_tlen);
    job.left_h0 = uint32_t(h0);
    job.right_qlen = uint32_t(right_qlen);
    job.right_tlen = uint32_t(right_tlen);
    try {
        if (valid) stage.jobs.push_back(job);
    } catch (const std::bad_alloc &) {
        stage.failed = true;
    }
    if (!valid || stage.failed) {
        stage.bases.resize(start);
        ++stage.skipped;
    }
}

// The GPU's extensions for the pair this thread is finishing.
struct ExtendCurrent {
    const ExtendBatch *batch = nullptr;
    const ExtendParameters *parameters = nullptr;
    const mem_opt_t *opt = nullptr;
    uint32_t first = 0, last = 0;
    // mem_chain2aln() extends chains in the order they were staged, so the
    // search for a call's job starts at the job of the previous hit.
    uint32_t cursor = 0;
};
thread_local ExtendCurrent extend_current;
struct ExtendTally {
    size_t skipped = 0, gpu_pairs = 0, cpu_pairs = 0, hits = 0, retry_hits = 0, misses = 0,
        mismatches = 0;
};
thread_local ExtendTally extend_tally;

inline int stride(const mem_opt_t *opt) {
    return (opt->flag & MEM_F_PE) ? 2 : 1;
}

// worker1 with GPU seeding and suffix-array locate, and with GPU seed
// extension if `extension` is given. Then a CPU thread chains each pair and
// stages the first extension of each chain in the shared extension batch; the
// GPU extends full batches (and partial ones when threads run out of work) on
// its own queue, and CPU threads finish those pairs, using the GPU's result for
// each extension whose inputs match exactly. Finishing pairs comes before
// mapping new ones.
class SeedScheduler : public Splitter {
public:
    std::atomic<size_t> missing{0};
    size_t extended_batches = 0, extended_jobs = 0;
    double extend_time = 0, extend_kernel_time = 0;

    SeedScheduler(const mem_opt_t *opt, const bwt_t *bwt, int n, bseq1_t *seqs,
        void (*worker)(void *, long, int), void *data, const ExtendParameters *extension)
        : Splitter(opt->n_threads, n / stride(opt), long(gpu->capacity) / stride(opt), false,
              inject_failure ? "injected GPU failure (test)" : nullptr, worker, data),
          opt_(opt), bwt_(bwt), seqs_(seqs), stride_(stride(opt)),
          prepared_(size_t(n / stride_) * stride_, nullptr), extend_(extension != nullptr) {
        if (!extend_) return;
        parameters_ = *extension;
        stages_.resize(size_t(opt->n_threads));
        for (auto &stage : stages_) {
            stage.match = opt->a;
            stage.max = parameters_.max;
            stage.e_ins = parameters_.e_ins;
        }
        for (auto &batch : extend_pool) free_.push_back(batch.get());
    }

    void align() {
        std::thread extension;
        if (extend_) {
            try {
                extension = std::thread(&SeedScheduler::extend_loop, this);
            } catch (const std::system_error &) {
                extend_ = false;
            }
        }
        run();
        if (!extension.joinable()) return;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        extend_changed_.notify_all();
        extension.join();
    }

private:
    const mem_opt_t *opt_;
    const bwt_t *bwt_;
    bseq1_t *seqs_;
    const int stride_;
    std::vector<const Prepared *> prepared_;
    bool extend_;
    ExtendParameters parameters_{};
    std::vector<ExtendStage> stages_;
    // Guarded by mutex_.
    ExtendBatch *filling_ = nullptr;
    std::vector<ExtendBatch *> free_;
    std::deque<ExtendBatch *> queued_, done_;
    // Pairs in extension batches that are not finished yet, and batches on the GPU.
    size_t pending_ = 0, in_flight_ = 0;
    bool stopping_ = false;
    std::condition_variable extend_changed_;

    void map(long pair, BatchBase *batch, int tid) override {
        if (!extend_) {
            Splitter::map(pair, batch, tid);
            return;
        }
        ExtendStage &stage = stages_[size_t(tid)];
        stage.bases.clear();
        stage.jobs.clear();
        stage.failed = false;
        begin(pair, batch);
        void *chains = wgse_worker1_chain(data_, pair, tid, stage_extension, &stage);
        // Out of memory before chaining: upstream's worker1 maps the pair instead.
        if (!chains) worker_(data_, pair, tid);
        end();
        if (!chains) return;
        extend_tally.skipped += stage.skipped;
        stage.skipped = 0;
        size_t base_offset = 0, job_offset = 0;
        ExtendBatch *target = stage.failed || stage.jobs.empty()
            ? nullptr : reserve(stage, pair, chains, base_offset, job_offset);
        if (!target) {
            if (!stage.jobs.empty()) ++extend_tally.cpu_pairs;
            finish(pair, chains, nullptr, 0, 0);
            return;
        }
        memcpy(static_cast<uint8_t *>(target->bases.contents) + base_offset, stage.bases.data(),
            stage.bases.size());
        auto *jobs = static_cast<ExtendJob *>(target->jobs.contents) + job_offset;
        for (ExtendJob job : stage.jobs) {
            job.left_query += uint32_t(base_offset);
            job.left_target += uint32_t(base_offset);
            job.right_query += uint32_t(base_offset);
            job.right_target += uint32_t(base_offset);
            *jobs++ = job;
        }
        std::lock_guard<std::mutex> lock(mutex_);
        if (--target->writers == 0 && target->sealed) queue(target);
    }

    bool fits(const ExtendBatch &batch, const ExtendStage &stage) const {
        return batch.pairs.size() < batch.pairs.capacity()
            && stage.bases.size() <= batch.base_capacity - batch.base_count
            && stage.jobs.size() <= batch.job_capacity - batch.job_count;
    }

    // Reserves room for the pair's jobs in the filling batch and returns it, or
    // null to extend the pair on the CPU now (no free batch, or GPU failed).
    ExtendBatch *reserve(const ExtendStage &stage, long pair, void *chains, size_t &base_offset,
        size_t &job_offset) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (filling_ && !fits(*filling_, stage)) seal();
        if (!filling_) {
            if (free_.empty() || !usable) return nullptr;
            filling_ = free_.back();
            free_.pop_back();
        }
        ExtendBatch *batch = filling_;
        // Only a pair with an extreme number of chains overflows an empty batch.
        if (!fits(*batch, stage)) return nullptr;
        base_offset = batch->base_count;
        job_offset = batch->job_count;
        batch->base_count += stage.bases.size();
        batch->job_count += stage.jobs.size();
        // pairs has the capacity for this (fits), so this does not allocate.
        batch->pairs.push_back({pair, chains, uint32_t(job_offset), uint32_t(batch->job_count)});
        ++batch->writers;
        ++pending_;
        if (batch->pairs.size() == batch->pairs.capacity()) seal();
        return batch;
    }

    // With mutex_ held: closes the filling batch.
    void seal() {
        ExtendBatch *batch = filling_;
        filling_ = nullptr;
        if (batch->pairs.empty()) {
            free_.push_back(batch);
            return;
        }
        batch->sealed = true;
        if (batch->writers == 0) queue(batch);
    }

    // With mutex_ held.
    void queue(ExtendBatch *batch) {
        batch->unfinished = batch->pairs.size();
        queued_.push_back(batch);
        extend_changed_.notify_one();
    }

    // Submits queued batches, at most extend_in_flight at once; their pairs are
    // finished when the GPU completes them (or right away after a failure).
    void extend_loop() {
        std::unique_lock<std::mutex> lock(mutex_);
        for (;;) {
            extend_changed_.wait(lock, [this] {
                return (!queued_.empty() && in_flight_ < extend_in_flight)
                    || (stopping_ && queued_.empty() && in_flight_ == 0);
            });
            if (queued_.empty()) return;
            ExtendBatch *batch = queued_.front();
            queued_.pop_front();
            ++in_flight_;
            lock.unlock();
            submit(batch);
            lock.lock();
        }
    }

    void submit(ExtendBatch *batch) {
        try {
            if (!usable) throw std::runtime_error("");
            if (inject_extend_failure)
                throw std::runtime_error("injected GPU extension failure (test)");
            const auto start = Clock::now();
            extender->submit(parameters_, batch->bases, batch->jobs, batch->results, batch->order,
                batch->job_count, batch->job_capacity, ^(id<MTLCommandBuffer> command) {
                    if (retry(batch, command)) return;
                    double kernel = 0;
                    bool extended = false;
                    try {
                        kernel = GpuExtend::finished(command);
                        extended = true;
                    } catch (const std::exception &error) {
                        record_failure(error.what());
                    }
                    complete(batch, extended, seconds(start), kernel);
                });
        } catch (const std::exception &error) {
            if (*error.what()) record_failure(error.what());
            complete(batch, false, 0, 0);
        }
    }

    // On the GPU's completion thread: queues an interrupted batch to be committed
    // again first (the kernels rewrite the results from the unchanged jobs).
    bool retry(ExtendBatch *batch, id<MTLCommandBuffer> command) {
        std::lock_guard<std::mutex> lock(mutex_);
        const int attempt = ++batch->attempts;
        if (attempt == wgse::metal::Runtime::attempts
            || !wgse::metal::Runtime::interrupted(command, attempt)) return false;
        queued_.push_front(batch);
        --in_flight_;
        extend_changed_.notify_one();
        return true;
    }

    // On the GPU's completion thread (or extend_loop's after a failure).
    void complete(ExtendBatch *batch, bool extended, double elapsed, double kernel) {
        std::lock_guard<std::mutex> lock(mutex_);
        batch->extended = extended;
        if (extended) {
            ++extended_batches;
            extended_jobs += batch->job_count;
            extend_time += elapsed;
            extend_kernel_time += kernel;
        }
        done_.push_back(batch);
        --in_flight_;
        changed_.notify_all();
        // Under the lock, so the scheduler outlives this call.
        extend_changed_.notify_one();
    }

    bool claim(Work &work) override {
        while (!done_.empty()) {
            ExtendBatch *batch = done_.front();
            if (batch->next < batch->pairs.size()) {
                work = {batch, batch->next++};
                if (batch->next == batch->pairs.size()) done_.pop_front();
                return true;
            }
            done_.pop_front();
        }
        return false;
    }

    void perform(const Work &work, int) override {
        auto &batch = *static_cast<ExtendBatch *>(work.owner);
        const ExtendBatch::Pair &entry = batch.pairs[work.index];
        ++(batch.extended ? extend_tally.gpu_pairs : extend_tally.cpu_pairs);
        finish(entry.pair, entry.chains, batch.extended ? &batch : nullptr, entry.first,
            entry.last);
        std::lock_guard<std::mutex> lock(mutex_);
        if (--batch.unfinished == 0) {
            batch.reset();
            free_.push_back(&batch);
        }
        if (--pending_ == 0) changed_.notify_all();
    }

    // Out of pairs to map: the partial batch goes to the GPU rather than wait.
    bool idle(int) override {
        if (!extend_) return false;
        if (filling_) seal();
        return pending_ > 0;
    }

    void finish(long pair, void *chains, const ExtendBatch *batch, uint32_t first, uint32_t last) {
        extend_current = {batch, &parameters_, opt_, first, last, first};
        wgse_worker1_finish(data_, pair, chains);
        extend_current = {};
    }

    void finish_thread() override {
        std::lock_guard<std::mutex> lock(totals_mutex);
        extend_skipped += extend_tally.skipped;
        extend_gpu_pairs += extend_tally.gpu_pairs;
        extend_cpu_pairs += extend_tally.cpu_pairs;
        extend_hits += extend_tally.hits;
        extend_retry_hits += extend_tally.retry_hits;
        extend_misses += extend_tally.misses;
        extend_mismatches += extend_tally.mismatches;
        extend_tally = {};
    }

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
        : Splitter(opt->n_threads, n / 2, long(rescue_batch_pairs), true, nullptr, worker, data),
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
        // The test failure comes after a published batch, with the next one packed.
        if (inject_rescue_failure && batches > 0)
            throw std::runtime_error("injected GPU rescue failure (test)");
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
// The extension kernel and its batches for opt->n_threads CPU threads.
void prepare_extension() {
    if (!extender)
        extender = std::make_unique<GpuExtend>(kernel().extend_runtime, kernel().extend);
    const size_t pairs = extend_batch_pairs;
    while (extend_pool.size() < extend_batch_count) {
        auto batch = std::make_unique<ExtendBatch>();
        batch->base_capacity = pairs * extend_bases_per_pair;
        batch->job_capacity = pairs * extend_jobs_per_pair;
        extender->allocate(batch->base_capacity, batch->job_capacity, batch->bases, batch->jobs,
            batch->results, batch->order);
        batch->pairs.reserve(pairs);
        extend_pool.push_back(std::move(batch));
    }
}
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
    const size_t pairs_before = pair_starts.load();
    const bool paired_at_start = pairs_running.load() != 0;
    @autoreleasepool {
        try {
            attach(bwt, opt, mapped_reads);
            mapped_reads += size_t(n);
            auto start = Clock::now();
            // bwa_verbose >= 4 prints each extension as mem_chain2aln runs it.
            ExtendParameters extension;
            const bool extend = extend_enabled && bwa_verbose < 4
                && wgse::extend::parameters(*opt, extension);
            if (extend) prepare_extension();
            SeedScheduler scheduler(opt, bwt, n, seqs, worker, data, extend ? &extension : nullptr);
            scheduler.align();
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
            extend_batches += scheduler.extended_batches;
            extend_jobs += scheduler.extended_jobs;
            extend_kernel_seconds += scheduler.extend_kernel_time;
            extend_gpu_seconds += scheduler.extend_time;
            if (paired_at_start || pairs_running.load() || pair_starts.load() != pairs_before)
                ++overlapped_chunks;
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
    if (enabled) ++pair_starts;
    if (!enabled || !usable || !rescue_enabled || !(opt->flag & MEM_F_PE)
        || (opt->flag & MEM_F_NO_RESCUE) || !wgse::rescue::parameters(*opt, parameters))
        return 0;
    @autoreleasepool {
        try {
            if (!rescuer) rescuer = std::make_unique<GpuRescue>(kernel().runtime, kernel().rescue);
            auto start = Clock::now();
            const double kernel_before = rescuer->kernel_seconds;
            RescueScheduler scheduler(opt, bns, pac, pes, n, seqs, regs, parameters, worker, data);
            struct Running {
                Running() { ++pairs_running; }
                ~Running() { --pairs_running; }
            } running;
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

extern "C" int wgse_metal_extend(int qlen, const uint8_t *query, int tlen, const uint8_t *target,
    int w, int end_bonus, int h0, int *qle, int *tle, int *gtle, int *gscore, int *max_off,
    int *score) {
    ExtendCurrent &pair = extend_current;
    if (!pair.batch) return 0;
    const ExtendParameters &p = *pair.parameters;
    const auto *jobs = static_cast<const ExtendJob *>(pair.batch->jobs.contents);
    const auto *results = static_cast<const ExtendResult *>(pair.batch->results.contents);
    const auto *bases = static_cast<const uint8_t *>(pair.batch->bases.contents);
    auto same = [](const uint8_t *a, const uint8_t *b, int length) {
        return length == 0 || memcmp(a, b, size_t(length)) == 0;
    };
    const ExtendCall *found = nullptr;
    const uint32_t count = pair.last - pair.first;
    for (uint32_t n = 0; n < count && !found; ++n) {
        uint32_t i = pair.cursor + n;
        if (i >= pair.last) i -= count;
        const ExtendJob &job = jobs[i];
        const ExtendResult &r = results[i];
        for (int side = 0; side < 2 && !found; ++side) {
            const uint32_t length = side ? job.right_qlen : job.left_qlen;
            const uint32_t window = side ? job.right_tlen : job.left_tlen;
            const int start = side ? r.right_h0 : int(job.left_h0);
            const int bonus = side ? p.right_bonus : p.left_bonus;
            const int tries = std::min<int>(side ? r.right_tries : r.left_tries, extend_tries);
            if (length != uint32_t(qlen) || window != uint32_t(tlen) || start != h0
                || bonus != end_bonus) continue;
            for (int t = 0; t < tries; ++t) {
                if (w != p.w << t) continue;
                if (same(bases + (side ? job.right_query : job.left_query), query, qlen)
                    && same(bases + (side ? job.right_target : job.left_target), target, tlen)) {
                    found = &r.calls[side * int(extend_tries) + t];
                    pair.cursor = i;
                    // A call with a doubled band.
                    if (t > 0) ++extend_tally.retry_hits;
                }
                break;
            }
        }
    }
    if (!found) {
        ++extend_tally.misses;
        return 0;
    }
    *score = found->score;
    *qle = found->qle;
    *tle = found->tle;
    *gtle = found->gtle;
    *gscore = found->gscore;
    *max_off = found->max_off;
    ++extend_tally.hits;
    if (verify_extend) {
        const mem_opt_t &opt = *pair.opt;
        int cpu[6];
        cpu[0] = ksw_extend2(qlen, query, tlen, target, 5, opt.mat, opt.o_del, opt.e_del,
            opt.o_ins, opt.e_ins, w, end_bonus, opt.zdrop, h0, &cpu[1], &cpu[2], &cpu[3], &cpu[4],
            &cpu[5]);
        const int gpu[6] = {*score, *qle, *tle, *gtle, *gscore, *max_off};
        if (memcmp(cpu, gpu, sizeof(cpu))) {
            if (extend_tally.mismatches++ < 3)
                fprintf(stderr, "[wgse-bwa] extension mismatch qlen=%d tlen=%d w=%d h0=%d "
                    "gpu=%d,%d,%d,%d,%d,%d cpu=%d,%d,%d,%d,%d,%d\n", qlen, tlen, w, h0, gpu[0],
                    gpu[1], gpu[2], gpu[3], gpu[4], gpu[5], cpu[0], cpu[1], cpu[2], cpu[3], cpu[4],
                    cpu[5]);
            *score = cpu[0];
            *qle = cpu[1];
            *tle = cpu[2];
            *gtle = cpu[3];
            *gscore = cpu[4];
            *max_off = cpu[5];
        }
    }
    return 1;
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
    if (!dense_low) return bwt_sa(bwt, k);
    // bwt_sa(), stopping at the first dense sample (locate_rows_dense).
    const bwtint_t base = bwtint_t(bwt->sa_intv) - 1, mask = (bwtint_t(1) << locate_dense_shift) - 1;
    bwtint_t steps = 0;
    while (k & mask) {
        ++steps;
        // bwt_invPsi(), which bwt.c keeps static.
        const bwtint_t x = bwt_B0(bwt, k - (k > bwt->primary));
        k = k == bwt->primary ? 0 : bwt->L2[x] + bwt_occ(bwt, k, ubyte_t(x));
    }
    if (!(k & base)) return steps + bwt->sa[k / bwtint_t(bwt->sa_intv)];
    const bwtint_t per = (bwtint_t(bwt->sa_intv) >> locate_dense_shift) - 1;
    const bwtint_t i = k / bwtint_t(bwt->sa_intv) * per + ((k & base) >> locate_dense_shift) - 1;
    return steps + (dense_low[i] | (bwtint_t((dense_high[i >> 5] >> (i & 31)) & 1) << 32));
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
        "wasted_reads=%zu unlocated_reads=%zu located_rows=%zu locate_misses=%zu "
        "overlapped_chunks=%zu interruptions=%zu\n", gpu_batches, gpu_reads, cpu_reads,
        fallback_reads, wasted_reads, unlocated_reads, located_rows, locate_misses.load(),
        overlapped_chunks, wgse::metal::Runtime::interruptions());
    fprintf(stderr, "[wgse-bwa] seconds gpu=%.3f locate=%.3f map=%.3f cpu_wait=%.3f\n",
        gpu_seconds, locate_seconds, map_seconds, cpu_wait_seconds);
    if (dense_tried)
        fprintf(stderr, "[wgse-bwa] locate_dense shift=%u built=%d build_seconds=%.3f\n",
            locate_dense_shift, dense_built ? 1 : 0, locate_dense_seconds);
    fprintf(stderr, "[wgse-bwa] rescue_batches=%zu rescue_gpu_pairs=%zu rescue_cpu_pairs=%zu "
        "rescue_wasted_pairs=%zu rescue_jobs=%zu rescue_declined=%zu rescue_skipped=%zu "
        "rescue_hits=%zu rescue_misses=%zu rescue_mismatches=%zu\n", rescue_batches,
        rescue_gpu_pairs, rescue_cpu_pairs, rescue_wasted_pairs, rescue_jobs, rescue_declined,
        rescue_skipped, rescue_hits, rescue_misses, rescue_mismatches);
    fprintf(stderr, "[wgse-bwa] rescue seconds gpu=%.3f kernel=%.3f pair=%.3f cpu_wait=%.3f\n",
        rescue_gpu_seconds, rescue_kernel_seconds, rescue_seconds, rescue_wait_seconds);
    fprintf(stderr, "[wgse-bwa] extend_batches=%zu extend_jobs=%zu extend_skipped=%zu "
        "extend_gpu_pairs=%zu extend_cpu_pairs=%zu extend_hits=%zu extend_retry_hits=%zu "
        "extend_misses=%zu extend_mismatches=%zu\n", extend_batches, extend_jobs, extend_skipped,
        extend_gpu_pairs, extend_cpu_pairs, extend_hits, extend_retry_hits, extend_misses,
        extend_mismatches);
    fprintf(stderr, "[wgse-bwa] extend seconds gpu=%.3f kernel=%.3f\n", extend_gpu_seconds,
        extend_kernel_seconds);
    extend_pool.clear();
    extender.reset();
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

void wgse_inject_gpu_interruptions(size_t every) {
    wgse::metal::Runtime::inject_interruptions(every);
}

void wgse_set_gpu_locate_rows(size_t rows) {
    locate_rows_per_batch = rows;
}

void wgse_set_gpu_locate_dense(unsigned shift) {
    locate_dense_shift = shift;
    locate_dense_forced = true;
}

void wgse_set_gpu_locate_dense_after(size_t reads) {
    locate_dense_after = reads;
}

void wgse_set_gpu_locate_dense_slice(size_t entries) {
    locate_dense_slice = entries;
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

void wgse_set_gpu_extend(bool enable) {
    extend_enabled = enable;
}

void wgse_set_gpu_extend_pairs(size_t pairs) {
    extend_batch_pairs = pairs;
}

void wgse_set_gpu_extend_batches(size_t batches) {
    extend_batch_count = batches;
}

size_t wgse_gpu_extend_pool_pairs() {
    return extend_batch_pairs * extend_batch_count;
}

void wgse_inject_extend_failure() {
    inject_extend_failure = true;
}

void wgse_verify_gpu_extend() {
    verify_extend = true;
}
