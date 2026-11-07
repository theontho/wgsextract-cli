#pragma once

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

namespace wgse::metal {

// GPU results for one batch of consecutive pairs.
struct BatchBase {
    // Published pairs not yet mapped; the last one frees the results, so large
    // chunks do not keep every batch until the chunk ends.
    std::atomic<long> pending{0};
    virtual ~BatchBase() = default;
    // Swapping (unlike assigning {}) also returns the vectors' capacity.
    virtual void release() = 0;
};

// Splits one chunk's pairs between the GPU and an upstream worker on n_threads
// CPU threads. The GPU prepares batches of pairs from the front; a CPU thread
// maps the next prepared pair if there is one, and otherwise an unprepared pair
// from the back with upstream's own code. Output does not depend on which side
// prepares a pair or on timing. (Adapted from wgse-bwa's metal_backend.mm.)
class Splitter {
public:
    using Clock = std::chrono::steady_clock;
    static double seconds(Clock::time_point start) {
        return std::chrono::duration<double>(Clock::now() - start).count();
    }

    // Totals for the metrics, in pairs, once run() returns.
    size_t prepared = 0, unprepared = 0, wasted = 0, batches = 0;
    double waited = 0, gpu_time = 0;

    // With overlap, the next batch is packed while the GPU runs the previous
    // one. With all_on_gpu (a test policy) CPU threads wait for the GPU instead
    // of mapping unprepared pairs while it works. At most max_outstanding
    // prepared batches wait for the CPU threads, which bounds memory.
    Splitter(int threads, long pairs, long pairs_per_batch, bool overlap, bool all_on_gpu,
        size_t max_outstanding, void (*worker)(void *, long, int), void *data)
        : worker_(worker), data_(data), threads_(threads),
          per_batch_(std::max<long>(1, pairs_per_batch)), overlap_(overlap),
          all_on_gpu_(all_on_gpu), max_outstanding_(std::max<size_t>(1, max_outstanding)),
          back_(pairs), owner_(size_t(pairs)) {}
    // A derived class whose GPU side may have started calls cancel_gpu() in its
    // own destructor, before its overrides go away.
    virtual ~Splitter() { cancel_gpu(); }

    // Starts the GPU side on a thread of its own before run(), e.g. while the
    // previous chunk maps; until run() it prepares at most max_outstanding
    // batches. If no thread starts, run() starts the GPU side as usual.
    void start_gpu() {
        try {
            gpu_thread_ = std::thread(&Splitter::gpu_loop, this);
        } catch (const std::system_error &) {
        }
    }

    // Stops a GPU side started by start_gpu() and waits for it.
    void cancel_gpu() {
        if (!gpu_thread_.joinable()) return;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            gpu_stopping_ = true;
        }
        drained_.notify_all();
        packed_changed_.notify_all();
        gpu_thread_.join();
    }

    void run(void (*worker)(void *, long, int), void *data) {
        worker_ = worker;
        data_ = data;
        run();
    }

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
        if (gpu_thread_.joinable()) {
            if (threads.empty()) cancel_gpu();
            else gpu_thread_.join();
        } else if (threads.empty()) {
            stop_gpu();
        } else {
            gpu_loop();
        }
        if (threads.size() < size_t(threads_)) cpu(static_cast<int>(threads.size()));
        for (auto &thread : threads) thread.join();
    }

protected:
    void (*worker_)(void *, long, int);
    void *data_;
    std::mutex mutex_;

    // Packs [first, last) for the GPU; CPU threads cannot take these pairs yet.
    virtual std::unique_ptr<BatchBase> pack(long first, long last) = 0;
    // The GPU work for a packed batch; may skip it if taken(first).
    virtual void prepare(BatchBase &batch, long first) = 0;
    // Around worker(pair) on its CPU thread; batch is null if the pair is unprepared.
    virtual void begin(long pair, BatchBase *batch) = 0;
    virtual void end() = 0;
    // On each CPU thread after its last pair.
    virtual void finish_thread() {}
    // A GPU failure; the CPU threads map the remaining pairs.
    virtual void failed(const std::string &reason) = 0;
    // When the GPU side has stopped preparing batches.
    virtual void gpu_finished() {}

    // Whether CPU threads have taken every pair of the batch that starts at first.
    bool taken(long first) {
        std::lock_guard<std::mutex> lock(mutex_);
        return back_ <= first;
    }

private:
    std::condition_variable changed_, drained_, packed_changed_;
    const int threads_;
    const long per_batch_;
    const bool overlap_, all_on_gpu_;
    const size_t max_outstanding_;
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
    // The batch of each pair in [0, ready_).
    std::vector<BatchBase *> owner_;
    std::vector<std::unique_ptr<BatchBase>> batches_;
    std::thread gpu_thread_;

    void cpu(int tid) {
        double wait_time = 0;
        size_t accelerated_pairs = 0, unprepared_pairs = 0, wasted_pairs = 0;
        for (;;) {
            long pair = -1;
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
                    const bool steal = gpu_stopped_ || !all_on_gpu_;
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

    // With overlap_, packs the next batch on a thread of its own (if one
    // starts) while the GPU runs the previous one; batches are published in pair
    // order either way.
    void gpu_loop() {
        auto start = Clock::now();
        try {
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
                while (pack_next() && prepare_next()) {
                }
            }
        } catch (const std::exception &error) {
            failed(error.what());
        }
        stop_gpu();
        gpu_time += seconds(start);
        gpu_finished();
    }

    void pack_loop() {
        try {
            while (pack_next()) {
            }
        } catch (const std::exception &error) {
            failed(error.what());
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
            failed(error.what());
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
                    || (outstanding_ + unpublished_ < max_outstanding_
                        && unpublished_ < (overlap_ ? 2u : 1u));
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

} // namespace wgse::metal
