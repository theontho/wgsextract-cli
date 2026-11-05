#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include "bandedSWA.h"
#include "extension_batch.h"
#include "kernel_source.h"
#include "metal_backend.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <new>
#include <pthread/qos.h>
#include <thread>
#include <time.h>
#include <utility>
#include <vector>

namespace {
constexpr size_t max_bytes = 64 * 1024 * 1024;
constexpr int32_t max_pairs = 65536;
constexpr int32_t max_query = 256;
constexpr int32_t max_target = 8192;
constexpr const char *tiled_memory_limit = "tiled extension exceeds memory bound";
std::atomic<Backend> selected_backend{Backend::automatic};
std::atomic<uint64_t> gpu_batches{0}, gpu_pairs{0}, cpu_batches{0}, cpu_pairs{0};
std::atomic<uint64_t> cpu_call_ns{0}, gpu_call_ns{0}, pack_ns{0}, gpu_exec_ns{0};
std::atomic<uint64_t> unpack_ns{0};
std::atomic<uint64_t> empty_pairs{0}, batch_sizes[6]{};
std::atomic<uint64_t> query_sizes[6]{};
std::atomic<uint64_t> gpu_cells{0};
std::atomic<uint64_t> issued_cells{0};
std::atomic<ExtensionKernel> selected_kernel{ExtensionKernel::adaptive};
std::atomic<int32_t> aggregation_target{8192}, aggregation_delay_us{100};
std::atomic<int32_t> aggregation_queue_pairs{32768};
std::atomic<int32_t> aggregation_target8{8192};
std::atomic<int32_t> minimum_pairs8{1};
std::atomic<bool> enable_gpu8{true};
std::atomic<bool> paired_gpu8{true};
std::atomic<bool> enable_pipelining{true};
std::atomic<uint64_t> help_ns{0}, help_calls{0}, blocked_ns{0};
thread_local WgseExtensionWork *worker_work = nullptr;
thread_local WgseExtensionScope *worker_scope = nullptr;
struct Request;
thread_local Request *worker_companion = nullptr;
void release_companion();
std::atomic<bool> force_gpu{false};
std::atomic<uint64_t> queue_ns{0}, wait_ns{0}, gpu_requests{0}, striped_batches{0};
std::atomic<uint64_t> metrics_start{0}, max_dispatch_pairs{0};
std::atomic<uint64_t> overlap_packs{0}, max_requests{0}, max_outstanding_pairs{0};
std::atomic<double> first_gpu_start{0}, last_gpu_end{0};
enum class CpuReason { selected, precision8, small, semantics, dimensions, busy, resource, count };
constexpr size_t reason_count = size_t(CpuReason::count);
struct Counter {
    std::atomic<uint64_t> batches{0}, pairs{0}, ns{0};
};
Counter cpu_reasons[reason_count][3], gpu_precisions[3];
std::atomic<uint64_t> mixed_dispatches{0}, mixed_gpu_ns{0};
thread_local CpuReason last_cpu_reason = CpuReason::selected;
thread_local int last_precision = 0, last_count = 0;
int precision_index(int precision) { return precision == 8 ? 1 : precision == 16 ? 2 : 0; }
int32_t target_pairs(int precision) {
    return precision == 8 ? aggregation_target8.load() : aggregation_target.load();
}
#ifdef WGSE_METAL_BENCHMARK_TOOLS
std::string capture_directory;
std::atomic<uint32_t> capture_count{0};

void capture_batch(SeqPair *pairs, const uint8_t *reference, const uint8_t *query,
    const ExtensionParameters &parameters) {
    if (capture_directory.empty() || parameters.count <= 0
        || parameters.count > max_pairs) return;
    uint32_t index = capture_count.fetch_add(1);
    if (index >= 4096) return;
    ExtensionBatch batch;
    batch.header.parameters = parameters;
    for (int i = 0; i < parameters.count; ++i) {
        SeqPair pair = pairs[i];
        if (pair.len1 < 0 || pair.len1 > max_target || pair.len2 < 0
            || pair.len2 > max_query || pair.idr < 0 || pair.idq < 0) return;
        pair.idr = static_cast<int32_t>(batch.reference.size());
        pair.idq = static_cast<int32_t>(batch.query.size());
        batch.reference.insert(batch.reference.end(), reference + pairs[i].idr,
            reference + pairs[i].idr + pair.len1);
        batch.query.insert(batch.query.end(), query + pairs[i].idq,
            query + pairs[i].idq + pair.len2);
        batch.pairs.push_back(pair);
    }
    batch.header.reference_bytes = batch.reference.size();
    batch.header.query_bytes = batch.query.size();
    char name[64];
    snprintf(name, sizeof(name), "/batch-%06u.bin", index);
    std::ofstream output(capture_directory + name, std::ios::binary);
    output.write(reinterpret_cast<const char *>(&batch.header), sizeof(batch.header));
    output.write(reinterpret_cast<const char *>(batch.pairs.data()),
        batch.pairs.size() * sizeof(SeqPair));
    output.write(reinterpret_cast<const char *>(batch.reference.data()), batch.reference.size());
    output.write(reinterpret_cast<const char *>(batch.query.data()), batch.query.size());
    if (!output) fprintf(stderr, "[wgse-bwa-mem2] Batch capture failed: %s\n", name);
}
#endif
thread_local bool last_call_gpu = false;

uint64_t now_ns() {
    return clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
}
std::atomic<bool> warned{false};
std::mutex execution_failure_mutex;
std::string execution_failure;
std::atomic<int32_t> automatic_inflight_limit{16}, automatic_minimum_pairs{128};
std::atomic<uint64_t> busy_batches{0};

using Parameters = ExtensionParameters;
static_assert(sizeof(Parameters) == 64);
static_assert(sizeof(SeqPair) == 56 && offsetof(SeqPair, score) == 32);
struct Job : SeqPair {
    int32_t bandwidth;
};
static_assert(sizeof(Job) == 60);

struct MetalState {
    id<MTLDevice> device = nil;
    id<MTLCommandQueue> queue = nil;
    id<MTLComputePipelineState> pipeline = nil, tiled_pipeline = nil, striped_pipeline = nil;
    id<MTLComputePipelineState> shared_pipeline = nil;
    std::string failure;
    bool hardware_supported = true;

    MetalState() {
        @autoreleasepool {
            device = MTLCreateSystemDefaultDevice();
            if (!device || !device.hasUnifiedMemory) {
                failure = "no unified-memory Metal device";
                hardware_supported = false;
                return;
            }
            NSError *error = nil;
            NSString *source = [NSString stringWithUTF8String:wgse_kernel_source];
            id<MTLLibrary> library = [device newLibraryWithSource:source
                options:nil error:&error];
            if (!library) {
                failure = error.localizedDescription.UTF8String;
                return;
            }
            id<MTLFunction> function = [library newFunctionWithName:@"extend_batch"];
            if (!function) {
                failure = "extension kernel missing";
                return;
            }
            pipeline = [device newComputePipelineStateWithFunction:function error:&error];
            id<MTLFunction> tiled = [library newFunctionWithName:@"extend_tiled"];
            MTLComputePipelineDescriptor *descriptor = [[MTLComputePipelineDescriptor alloc] init];
            descriptor.maxTotalThreadsPerThreadgroup = 32;
            descriptor.threadGroupSizeIsMultipleOfThreadExecutionWidth = YES;
            if (tiled) {
                descriptor.computeFunction = tiled;
                tiled_pipeline = [device newComputePipelineStateWithDescriptor:descriptor
                    options:MTLPipelineOptionNone reflection:nil error:&error];
            }
            id<MTLFunction> striped = [library newFunctionWithName:@"extend_striped"];
            if (striped) {
                descriptor.computeFunction = striped;
                striped_pipeline = [device newComputePipelineStateWithDescriptor:descriptor
                    options:MTLPipelineOptionNone reflection:nil error:&error];
            }
            id<MTLFunction> shared = [library newFunctionWithName:@"extend_shared"];
            if (shared) {
                descriptor.computeFunction = shared;
                shared_pipeline = [device newComputePipelineStateWithDescriptor:descriptor
                    options:MTLPipelineOptionNone reflection:nil error:&error];
            }
            queue = [device newCommandQueue];
            if (!pipeline || !tiled_pipeline || !striped_pipeline || !shared_pipeline || !queue) {
                failure = error ? error.localizedDescription.UTF8String
                    : "Metal command queue allocation failed";
            } else if (pipeline.threadExecutionWidth != 32) {
                failure = "extension kernel requires 32-lane SIMD groups";
                hardware_supported = false;
            }
        }
    }
};

MetalState &state() {
    static MetalState value;
    return value;
}

struct Workspace {
    id<MTLBuffer> pairs = nil, reference = nil, query = nil;
    id<MTLBuffer> packed_reference = nil, packed_query = nil, scratch = nil;
};

bool reserve(id<MTLDevice> device, id<MTLBuffer> __strong &buffer, size_t bytes) {
    if (buffer && buffer.length >= bytes) return true;
    size_t capacity = 64 * 1024;
    while (capacity < bytes) capacity *= 2;
    buffer = [device newBufferWithLength:capacity
        options:MTLResourceStorageModeShared | MTLResourceHazardTrackingModeUntracked];
    return buffer != nil;
}

bool fallback(const std::string &reason, bool report, CpuReason category = CpuReason::resource) {
    release_companion();
    ++cpu_batches;
    last_call_gpu = false;
    last_cpu_reason = category;
    auto &counter = cpu_reasons[size_t(category)][precision_index(last_precision)];
    ++counter.batches;
    counter.pairs += uint64_t(std::max(0, last_count));
    if (report && !warned.exchange(true))
        fprintf(stderr, "[wgse-bwa-mem2] CPU fallback: %s\n", reason.c_str());
    return false;
}

bool same_scoring(const Parameters &a, const Parameters &b) {
    return a.open_del == b.open_del && a.extend_del == b.extend_del
        && a.open_ins == b.open_ins && a.extend_ins == b.extend_ins
        && a.zdrop == b.zdrop && a.end_bonus == b.end_bonus
        && a.max_substitution == b.max_substitution
        // Validated SIMD8/16 jobs have identical wide shader arithmetic.
        && (a.precision == 0) == (b.precision == 0)
        && a.simple_matrix == b.simple_matrix
        && memcmp(a.matrix, b.matrix, sizeof(a.matrix)) == 0;
}

struct Request {
    SeqPair *pairs;
    const uint8_t *reference, *query;
    int32_t count, active;
    size_t reference_bytes, query_bytes;
    Parameters parameters;
    Request *next = nullptr;
    uint64_t enqueued = 0;
    bool done = false;
    std::string failure;
    std::condition_variable completed;
    bool ready = true;
    Request *partner = nullptr;
};

struct Slot {
    Workspace work;
    std::vector<Request *> requests;
    std::vector<SeqPair *> destinations;
    Parameters parameters{};
    ExtensionKernel kernel = ExtensionKernel::row;
    id<MTLBuffer> reference = nil, query = nil;
    id<MTLCommandBuffer> command = nil;
    enum { free, packing, ready, executing, completed } status = free;
    std::string failure;
};

// Only the submitter touches the two grow-only workspaces. Workers retain their
// input storage until their own request completes. The next slot is packed while
// the GPU executes the previous one; there is never more than one GPU dispatch.
class Aggregator {
    std::mutex mutex;
    std::condition_variable changed;
    Request *pending[3]{};
    Slot slots[2];
    int32_t waiting = 0, outstanding_pairs = 0;
    bool stopping = false;
    std::thread submitter;

    Request *oldest() {
        Request *result = nullptr;
        for (auto *head : pending)
            for (auto *request = head; request; request = request->next)
                if (request->ready && (!result || request->enqueued < result->enqueued))
                    result = request;
        return result;
    }

    int32_t compatible_pairs(const Parameters &parameters) {
        int32_t total = 0;
        for (auto *head : pending)
            for (auto *request = head; request; request = request->next)
                if (request->ready && same_scoring(parameters, request->parameters))
                    total += request->active;
        return total;
    }

    void take(Slot &slot, const Parameters &parameters) {
        slot.parameters = parameters;
        slot.parameters.count = 0;
        slot.requests.clear();
        slot.destinations.clear();
        slot.failure.clear();
        size_t bytes = 0;
        for (auto &head : pending) {
            for (auto **link = &head; *link;) {
                Request *request = *link;
                size_t added = request->reference_bytes + request->query_bytes
                    + size_t(request->active) * sizeof(Job);
                Request *partner = request->partner;
                int32_t count = request->active;
                if (partner) {
                    added += partner->reference_bytes + partner->query_bytes
                        + size_t(partner->active) * sizeof(Job);
                    count += partner->active;
                }
                if (!request->ready || !same_scoring(parameters, request->parameters)
                    || slot.parameters.count + count > max_pairs
                    || bytes + added > max_bytes) {
                    link = &request->next;
                    continue;
                }
                *link = request->next;
                slot.requests.push_back(request);
                slot.parameters.count += count;
                if (partner) {
                    for (auto &other : pending) {
                        for (auto **p = &other; *p; p = &(*p)->next) {
                            if (*p != partner) continue;
                            *p = partner->next;
                            break;
                        }
                    }
                    slot.requests.push_back(partner);
                }
                if (request->parameters.precision == 16 || partner)
                    slot.parameters.precision = 16;
                bytes += added;
                if (slot.parameters.count >= target_pairs(parameters.precision)) return;
            }
        }
    }

    bool pack(Slot &slot) {
        auto &metal = state();
        auto &work = slot.work;
        int32_t count = slot.parameters.count;
        size_t reference_bytes = 0, query_bytes = 0;
        for (auto *request : slot.requests) {
            reference_bytes += request->reference_bytes;
            query_bytes += request->query_bytes;
        }
        if (!reserve(metal.device, work.pairs, size_t(count) * sizeof(Job))
            || !reserve(metal.device, work.reference, reference_bytes)
            || !reserve(metal.device, work.query, query_bytes)) {
            slot.failure = "Metal buffer allocation failed";
            return false;
        }
        auto *jobs = static_cast<Job *>(work.pairs.contents);
        auto *reference = static_cast<uint8_t *>(work.reference.contents);
        auto *query = static_cast<uint8_t *>(work.query.contents);
        size_t rbase = 0, qbase = 0;
        int32_t index = 0;
        for (auto *request : slot.requests) {
            for (int32_t i = 0; i < request->count; ++i) {
                auto &pair = request->pairs[i];
                if (pair.len1 == 0 || pair.len2 == 0) continue;
                auto &job = jobs[index];
                static_cast<SeqPair &>(job) = pair;
                job.bandwidth = request->parameters.bandwidth;
                job.seqid = index++;
                job.idr = static_cast<int32_t>(rbase);
                job.idq = static_cast<int32_t>(qbase);
                slot.destinations.push_back(&pair);
                memcpy(reference + rbase, request->reference + pair.idr, size_t(pair.len1));
                memcpy(query + qbase, request->query + pair.idq, size_t(pair.len2));
                rbase += size_t(pair.len1);
                qbase += size_t(pair.len2);
            }
        }
        slot.reference = work.reference;
        slot.query = work.query;
        auto kernel = selected_kernel.load();
        if (kernel == ExtensionKernel::adaptive)
            kernel = count >= (slot.parameters.precision == 8 ? 8192 : 2048)
                ? ExtensionKernel::striped : ExtensionKernel::row;
        if (kernel == ExtensionKernel::shared)
            for (int i = 0; i < count; ++i)
                if (jobs[i].len2 > 128) kernel = ExtensionKernel::striped;
        slot.kernel = kernel;
        if (kernel == ExtensionKernel::row) return true;
        std::sort(jobs, jobs + count, [&slot](const Job &a, const Job &b) {
            if (slot.parameters.precision == 8 && a.h0 / 8 != b.h0 / 8)
                return a.h0 / 8 < b.h0 / 8;
            if (a.len2 / 8 != b.len2 / 8) return a.len2 / 8 < b.len2 / 8;
            return a.len1 < b.len1;
        });
        size_t reference_words = 0, query_words = 0, scratch_cells = 0;
        for (int first = 0; first < count; first += 32) {
            int r = 0, q = 0;
            for (int i = first; i < std::min(count, first + 32); ++i) {
                r = std::max(r, (jobs[i].len1 + 7) / 8);
                q = std::max(q, (jobs[i].len2 + 7) / 8);
            }
            reference_words += size_t(r) * 32;
            query_words += size_t(q) * 32;
            scratch_cells += size_t(q * 8 + 1) * 64;
        }
        if ((reference_words + query_words + scratch_cells) * 4
            + size_t(count) * sizeof(Job) > max_bytes) {
            slot.failure = tiled_memory_limit;
            return false;
        }
        if (!reserve(metal.device, work.packed_reference, reference_words * 4)
            || !reserve(metal.device, work.packed_query, query_words * 4)
            || !reserve(metal.device, work.scratch, scratch_cells * 4)) {
            slot.failure = "tiled buffer allocation failed";
            return false;
        }
        auto *packed_reference = static_cast<uint32_t *>(work.packed_reference.contents);
        auto *packed_query = static_cast<uint32_t *>(work.packed_query.contents);
        rbase = qbase = 0;
        size_t sbase = 0;
        for (int first = 0; first < count; first += 32) {
            int rmax = 0, qmax = 0;
            for (int i = first; i < std::min(count, first + 32); ++i) {
                auto &job = jobs[i];
                int lane = i - first;
                const uint8_t *r = reference + job.idr, *q = query + job.idq;
                job.idr = static_cast<int32_t>(rbase) + lane;
                job.idq = static_cast<int32_t>(qbase) + lane;
                job.id = static_cast<int32_t>(sbase) + lane;
                rmax = std::max(rmax, (job.len1 + 7) / 8);
                qmax = std::max(qmax, (job.len2 + 7) / 8);
                for (int j = 0; j < job.len1; j += 8) {
                    uint32_t word = 0;
                    for (int k = 0; k < 8 && j + k < job.len1; ++k)
                        word |= uint32_t(r[j + k]) << (k * 4);
                    packed_reference[rbase + lane + size_t(j / 8) * 32] = word;
                }
                for (int j = 0; j < job.len2; j += 8) {
                    uint32_t word = 0;
                    for (int k = 0; k < 8 && j + k < job.len2; ++k)
                        word |= uint32_t(q[j + k]) << (k * 4);
                    packed_query[qbase + lane + size_t(j / 8) * 32] = word;
                }
            }
            for (int i = first; i < std::min(count, first + 32); ++i)
                jobs[i].regid = (qmax * 8 + 1) * 32;
            rbase += size_t(rmax) * 32;
            qbase += size_t(qmax) * 32;
            sbase += size_t(qmax * 8 + 1) * 64;
        }
        slot.reference = work.packed_reference;
        slot.query = work.packed_query;
        return true;
    }

    void dispatch(Slot &slot) {
        auto &metal = state();
        auto kernel = slot.kernel;
        bool tiled = kernel != ExtensionKernel::row;
        slot.command = [metal.queue commandBufferWithUnretainedReferences];
        id<MTLComputeCommandEncoder> encoder = [slot.command computeCommandEncoder];
        if (!slot.command || !encoder) {
            slot.failure = "Metal command allocation failed";
            std::lock_guard<std::mutex> lock(mutex);
            slot.status = Slot::completed;
            return;
        }
        [encoder setComputePipelineState:kernel == ExtensionKernel::row ? metal.pipeline
            : kernel == ExtensionKernel::striped ? metal.striped_pipeline
            : kernel == ExtensionKernel::shared ? metal.shared_pipeline : metal.tiled_pipeline];
        [encoder setBuffer:slot.work.pairs offset:0 atIndex:0];
        [encoder setBuffer:slot.reference offset:0 atIndex:1];
        [encoder setBuffer:slot.query offset:0 atIndex:2];
        [encoder setBytes:&slot.parameters length:sizeof(Parameters) atIndex:3];
        if (tiled) [encoder setBuffer:slot.work.scratch offset:0 atIndex:4];
        // Tiled pipelines promise whole 32-lane groups; their kernels skip
        // the padding threads past the batch count.
        NSUInteger count = NSUInteger(slot.parameters.count);
        [encoder dispatchThreads:MTLSizeMake(tiled ? (count + 31) / 32 * 32 : count * 32, 1, 1)
            threadsPerThreadgroup:MTLSizeMake(tiled ? 32 : 128, 1, 1)];
        [encoder endEncoding];
        uint64_t submitted = now_ns();
        for (auto *request : slot.requests) queue_ns += submitted - request->enqueued;
        Slot *completed_slot = &slot;
        [slot.command addCompletedHandler:^(id<MTLCommandBuffer>) {
            @autoreleasepool { finish(*completed_slot); }
        }];
        [slot.command commit];
    }

    void finish(Slot &slot) {
        if (slot.failure.empty() && slot.command.status != MTLCommandBufferStatusCompleted)
            slot.failure = slot.command.error ? slot.command.error.localizedDescription.UTF8String
                : "Metal command did not complete";
        if (slot.failure.empty()) {
            double start = slot.command.GPUStartTime, end = slot.command.GPUEndTime;
            if (first_gpu_start == 0) first_gpu_start = start;
            last_gpu_end = end;
            gpu_exec_ns += static_cast<uint64_t>((end - start) * 1e9);
            uint64_t counts[3]{};
            for (auto *request : slot.requests)
                counts[precision_index(request->parameters.precision)] += uint64_t(request->active);
            bool mixed = counts[1] && counts[2];
            for (int i = 0; i < 3; ++i) {
                if (!counts[i]) continue;
                ++gpu_precisions[i].batches;
                gpu_precisions[i].pairs += counts[i];
                if (!mixed) gpu_precisions[i].ns += static_cast<uint64_t>((end - start) * 1e9);
            }
            if (mixed) {
                ++mixed_dispatches;
                mixed_gpu_ns += static_cast<uint64_t>((end - start) * 1e9);
            }
            uint64_t unpack_start = now_ns(), cells = 0, issued = 0;
            auto *jobs = static_cast<Job *>(slot.work.pairs.contents);
            for (int32_t i = 0; i < slot.parameters.count; ++i) {
                auto &job = jobs[i];
                auto &pair = *slot.destinations[job.seqid];
                pair.score = job.score;
                pair.tle = job.tle;
                pair.gtle = job.gtle;
                pair.qle = job.qle;
                pair.gscore = job.gscore;
                pair.max_off = job.max_off;
                cells += static_cast<uint64_t>(job.regid);
                issued += static_cast<uint64_t>(job.idr);
            }
            gpu_cells += cells;
            issued_cells += issued;
            unpack_ns += now_ns() - unpack_start;
            ++gpu_batches;
            gpu_requests += slot.requests.size();
            gpu_pairs += static_cast<uint64_t>(slot.parameters.count);
            if (slot.kernel == ExtensionKernel::striped) ++striped_batches;
            max_dispatch_pairs = std::max(max_dispatch_pairs.load(),
                uint64_t(slot.parameters.count));
        }
        uint64_t completed = now_ns();
        std::lock_guard<std::mutex> lock(mutex);
        for (auto *request : slot.requests) {
            request->failure = slot.failure;
            request->done = true;
            wait_ns += completed - request->enqueued;
            --waiting;
            outstanding_pairs -= request->active;
            request->completed.notify_one();
        }
        slot.command = nil;
        slot.status = Slot::free;
        changed.notify_one();
    }

    void run() {
        pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0);
        std::unique_lock<std::mutex> lock(mutex);
        for (;;) {
            Slot *free = nullptr, *ready = nullptr, *completed = nullptr;
            bool executing = false;
            for (auto &slot : slots) {
                if (slot.status == Slot::free) free = &slot;
                if (slot.status == Slot::ready) ready = &slot;
                if (slot.status == Slot::completed) completed = &slot;
                executing |= slot.status == Slot::executing;
            }
            if (completed) {
                lock.unlock();
                @autoreleasepool { finish(*completed); }
                lock.lock();
            } else if (ready && !executing) {
                ready->status = Slot::executing;
                lock.unlock();
                @autoreleasepool { dispatch(*ready); }
                lock.lock();
            } else if (free && oldest()) {
                Request *request = oldest();
                uint64_t deadline = request->enqueued + uint64_t(aggregation_delay_us) * 1000;
                uint64_t now = now_ns();
                if (compatible_pairs(request->parameters) < target_pairs(request->parameters.precision)
                    && (executing || now < deadline)) {
                    if (executing) changed.wait(lock);
                    else changed.wait_for(lock, std::chrono::nanoseconds(deadline - now));
                    continue;
                }
                take(*free, request->parameters);
                free->status = Slot::packing;
                if (executing) ++overlap_packs;
                lock.unlock();
                uint64_t start = now_ns();
                bool success;
                @autoreleasepool { success = pack(*free); }
                pack_ns += now_ns() - start;
                lock.lock();
                free->status = success ? Slot::ready : Slot::completed;
            } else if (stopping && waiting == 0) {
                return;
            } else {
                changed.wait(lock);
            }
        }
    }

public:
    Aggregator() : submitter([this] { run(); }) {}
    ~Aggregator() {
        {
            std::lock_guard<std::mutex> lock(mutex);
            stopping = true;
            changed.notify_all();
        }
        submitter.join();
    }

    void release(Request &request) {
        std::lock_guard<std::mutex> lock(mutex);
        request.ready = true;
        changed.notify_one();
    }

    bool extend(Request &request, bool hybrid) {
        std::unique_lock<std::mutex> lock(mutex);
        Request *partner = worker_companion;
        if (worker_companion) {
            worker_companion->ready = true;
            worker_companion = nullptr;
            changed.notify_one();
        }
        bool can_pair = partner && request.parameters.precision == 8
            && partner->parameters.precision == 16
            && same_scoring(request.parameters, partner->parameters)
            && request.active + partner->active <= max_pairs
            && request.reference_bytes + request.query_bytes + partner->reference_bytes
                + partner->query_bytes + size_t(request.active + partner->active) * sizeof(Job)
                <= max_bytes;
        if (hybrid && paired_gpu8 && request.parameters.precision == 8 && !can_pair)
            return fallback("8-bit launch would add a separate wait", false, CpuReason::precision8);
        if (hybrid && (waiting >= automatic_inflight_limit
            || outstanding_pairs >= aggregation_queue_pairs)) {
            ++busy_batches;
            return fallback("GPU busy", false, CpuReason::busy);
        }
        if (can_pair) {
            request.partner = partner;
            partner->partner = &request;
        }
        int pool = request.parameters.precision == 8 ? 1
            : request.parameters.precision == 16 ? 2 : 0;
        Request **tail = &pending[pool];
        while (*tail) tail = &(*tail)->next;
        *tail = &request;
        request.enqueued = now_ns();
        ++waiting;
        outstanding_pairs += request.active;
        max_requests = std::max(max_requests.load(), uint64_t(waiting));
        max_outstanding_pairs = std::max(max_outstanding_pairs.load(), uint64_t(outstanding_pairs));
        changed.notify_one();
        if (worker_work && request.parameters.precision == 16) {
            auto *work = worker_work;
            request.ready = false;
            worker_companion = &request;
            int precision = last_precision, count = last_count;
            auto reason = last_cpu_reason;
            bool gpu = last_call_gpu;
            lock.unlock();
            uint64_t start = now_ns();
            try {
                work->finish(true);
            } catch (...) {
                lock.lock();
                request.ready = true;
                worker_companion = nullptr;
                changed.notify_one();
                request.completed.wait(lock, [&request] { return request.done; });
                throw;
            }
            help_ns += now_ns() - start;
            ++help_calls;
            last_precision = precision;
            last_count = count;
            last_cpu_reason = reason;
            last_call_gpu = gpu;
            lock.lock();
            request.ready = true;
            worker_companion = nullptr;
            changed.notify_one();
        }
        uint64_t wait_start = now_ns();
        request.completed.wait(lock, [&request] { return request.done; });
        blocked_ns += now_ns() - wait_start;
        if (request.failure.empty()) return true;
        if (request.failure != tiled_memory_limit) {
            std::lock_guard<std::mutex> failure_lock(execution_failure_mutex);
            if (execution_failure.empty()) execution_failure = request.failure;
        }
        return fallback(request.failure, true);
    }
};

Aggregator &aggregator() {
    static Aggregator value;
    return value;
}
void release_companion() {
    if (!worker_companion) return;
    auto *request = worker_companion;
    worker_companion = nullptr;
    aggregator().release(*request);
}
}

bool wgse_metal_available(std::string &reason) {
    reason = state().failure;
    return reason.empty();
}

bool wgse_metal_hardware_supported() { return state().hardware_supported; }

bool wgse_metal_execution_failed(std::string &reason) {
    std::lock_guard<std::mutex> lock(execution_failure_mutex);
    reason = execution_failure;
    return !reason.empty();
}

void wgse_set_backend(Backend backend) {
    selected_backend = backend;
    uint64_t unset = 0;
    metrics_start.compare_exchange_strong(unset, now_ns());
}

uint64_t wgse_gpu_execution_ns() {
    return gpu_exec_ns.load();
}
uint64_t wgse_gpu_dispatches() { return gpu_batches.load(); }

uint64_t wgse_gpu_cells() { return gpu_cells.load(); }
uint64_t wgse_gpu_issued_cells() { return issued_cells.load(); }
void wgse_set_kernel(ExtensionKernel kernel) { selected_kernel = kernel; }
void wgse_set_routing(int32_t minimum_pairs, int32_t inflight_limit) {
    automatic_minimum_pairs = minimum_pairs;
    automatic_inflight_limit = inflight_limit;
}
void wgse_set_aggregation(int32_t target, int32_t delay_us, int32_t queue_pairs) {
    aggregation_target = std::clamp(target, 1, max_pairs);
    aggregation_delay_us = std::clamp(delay_us, 0, 100000);
    aggregation_queue_pairs = std::clamp(queue_pairs, 1, max_pairs * 2);
}
void wgse_set_force_gpu(bool force) { force_gpu = force; }
void wgse_set_gpu8(bool enabled, int32_t target, int32_t minimum_pairs, bool paired) {
    enable_gpu8 = enabled;
    aggregation_target8 = std::clamp(target, 1, max_pairs);
    minimum_pairs8 = std::clamp(minimum_pairs, 1, max_pairs);
    paired_gpu8 = paired;
}
void wgse_set_pipelining(bool enabled) { enable_pipelining = enabled; }

WgseExtensionScratch wgse_extension_scratch(
    const SeqPair *pairs, int32_t count, int32_t histogram_size) {
    struct Buffers {
        SeqPair *pairs = nullptr, *aux = nullptr;
        size_t capacity = 0;
        std::vector<int32_t> hist;
        ~Buffers() { _mm_free(pairs); _mm_free(aux); }
    };
    static thread_local Buffers buffers;
    size_t needed = size_t(count) + 64;
    if (needed > buffers.capacity) {
        auto *input = static_cast<SeqPair *>(_mm_malloc(needed * sizeof(SeqPair), 64));
        auto *aux = static_cast<SeqPair *>(_mm_malloc(needed * sizeof(SeqPair), 64));
        if (!input || !aux) {
            _mm_free(input);
            _mm_free(aux);
            throw std::bad_alloc();
        }
        _mm_free(buffers.pairs);
        _mm_free(buffers.aux);
        buffers.pairs = input;
        buffers.aux = aux;
        buffers.capacity = needed;
    }
    if (buffers.hist.size() < size_t(histogram_size)) buffers.hist.resize(size_t(histogram_size));
    memcpy(buffers.pairs, pairs, size_t(count) * sizeof(SeqPair));
    memset(buffers.pairs + count, 0, 64 * sizeof(SeqPair));
    memset(buffers.aux + count, 0, 64 * sizeof(SeqPair));
    return {buffers.pairs, buffers.aux, buffers.hist.data()};
}

WgseExtensionWork::WgseExtensionWork(std::function<void(bool)> function)
    : work(std::move(function)), previous(worker_work) {
    if (enable_pipelining && selected_backend != Backend::cpu) worker_work = this;
}
WgseExtensionWork::~WgseExtensionWork() { finish(); }
void WgseExtensionWork::finish(bool independent) {
    if (done) return;
    done = true;
    if (worker_work == this) worker_work = previous;
    work(independent);
}

#ifdef WGSE_METAL_BENCHMARK_TOOLS
bool wgse_set_capture_directory(const std::string &path, std::string &reason) {
    try {
        if (std::filesystem::exists(path) && !std::filesystem::is_empty(path))
            throw std::runtime_error("Capture directory must be empty");
        std::filesystem::create_directories(path);
        capture_directory = path;
        capture_count = 0;
        return true;
    } catch (const std::exception &error) {
        reason = error.what();
        return false;
    }
}
#endif

WgseExtensionScope::WgseExtensionScope(int32_t pairs)
    : start(now_ns()), count(pairs), parent(worker_scope) {
    last_call_gpu = false;
    worker_scope = this;
}

WgseExtensionScope::~WgseExtensionScope() {
    uint64_t elapsed = now_ns() - start;
    worker_scope = parent;
    if (parent) parent->children += elapsed;
    elapsed -= children;
    if (last_call_gpu) {
        gpu_call_ns += elapsed;
    } else {
        cpu_call_ns += elapsed;
        cpu_pairs += static_cast<uint64_t>(count > 0 ? count : 0);
        cpu_reasons[size_t(last_cpu_reason)][precision_index(last_precision)].ns += elapsed;
    }
}

void wgse_print_backend_metrics() {
    auto ms = [](const std::atomic<uint64_t> &value) {
        return static_cast<double>(value.load()) / 1e6;
    };
    fprintf(stderr,
        "[wgse-bwa-mem2] gpu_batches=%llu gpu_pairs=%llu cpu_batches=%llu"
        " cpu_pairs=%llu\n",
        static_cast<unsigned long long>(gpu_batches.load()),
        static_cast<unsigned long long>(gpu_pairs.load()),
        static_cast<unsigned long long>(cpu_batches.load()),
        static_cast<unsigned long long>(cpu_pairs.load()));
    fprintf(stderr,
        "[wgse-bwa-mem2] thread_ms cpu_extension=%.1f gpu_extension=%.1f"
        " pack=%.1f gpu_exec=%.1f unpack=%.1f\n",
        ms(cpu_call_ns), ms(gpu_call_ns), ms(pack_ns), ms(gpu_exec_ns),
        ms(unpack_ns));
    fprintf(stderr, "[wgse-bwa-mem2] gpu_busy_cpu_batches=%llu\n",
        static_cast<unsigned long long>(busy_batches.load()));
    fprintf(stderr, "[wgse-bwa-mem2] pipeline_calls=%llu help_thread_ms=%.1f"
        " blocked_thread_ms=%.1f\n",
        static_cast<unsigned long long>(help_calls.load()), ms(help_ns), ms(blocked_ns));
    constexpr const char *names[] = {
        "selected", "precision8", "small", "semantics", "dimensions", "busy", "resource"};
    for (size_t reason = 0; reason < reason_count; ++reason) {
        const auto &a = cpu_reasons[reason][1], &b = cpu_reasons[reason][2];
        fprintf(stderr, "[wgse-bwa-mem2] cpu_category=%s pairs8=%llu pairs16=%llu"
            " pairs_other=%llu thread_ms8=%.1f thread_ms16=%.1f\n",
            names[reason], static_cast<unsigned long long>(a.pairs.load()),
            static_cast<unsigned long long>(b.pairs.load()),
            static_cast<unsigned long long>(cpu_reasons[reason][0].pairs.load()),
            ms(a.ns), ms(b.ns));
    }
    for (int bits : {8, 16}) {
        const auto &counter = gpu_precisions[precision_index(bits)];
        uint64_t batches = counter.batches;
        fprintf(stderr, "[wgse-bwa-mem2] gpu_precision=%d pairs=%llu dispatches=%llu"
            " pairs_per_dispatch=%.1f exclusive_gpu_ms=%.1f\n", bits,
            static_cast<unsigned long long>(counter.pairs.load()),
            static_cast<unsigned long long>(batches),
            batches ? double(counter.pairs.load()) / batches : 0, ms(counter.ns));
    }
    fprintf(stderr, "[wgse-bwa-mem2] mixed_dispatches=%llu mixed_gpu_ms=%.1f\n",
        static_cast<unsigned long long>(mixed_dispatches.load()), ms(mixed_gpu_ns));
    uint64_t dispatches = gpu_batches.load(), requests = gpu_requests.load();
    double elapsed = metrics_start ? double(now_ns() - metrics_start) / 1e9 : 0;
    double gpu_seconds = double(gpu_exec_ns.load()) / 1e9;
    fprintf(stderr, "[wgse-bwa-mem2] aggregation requests=%llu pairs_per_dispatch=%.1f"
        " max_pairs=%llu striped_dispatches=%llu gpu_busy_fraction=%.4f"
        " gpu_span_busy_fraction=%.4f queue_us=%.1f wait_us=%.1f elapsed_s=%.3f\n",
        static_cast<unsigned long long>(requests),
        dispatches ? double(gpu_pairs.load()) / dispatches : 0,
        static_cast<unsigned long long>(max_dispatch_pairs.load()),
        static_cast<unsigned long long>(striped_batches.load()),
        elapsed > 0 ? gpu_seconds / elapsed : 0,
        last_gpu_end > first_gpu_start ? gpu_seconds / (last_gpu_end - first_gpu_start) : 0,
        requests ? double(queue_ns.load()) / requests / 1000 : 0,
        requests ? double(wait_ns.load()) / requests / 1000 : 0, elapsed);
    fprintf(stderr, "[wgse-bwa-mem2] queue_peak_requests=%llu queue_peak_pairs=%llu"
        " pack_started_while_executing=%llu target_pairs=%d queue_pairs=%d delay_us=%d\n",
        static_cast<unsigned long long>(max_requests.load()),
        static_cast<unsigned long long>(max_outstanding_pairs.load()),
        static_cast<unsigned long long>(overlap_packs.load()),
        aggregation_target.load(), aggregation_queue_pairs.load(), aggregation_delay_us.load());
    fprintf(stderr, "[wgse-bwa-mem2] empty_pairs=%llu active_batch_bins="
        "%llu,%llu,%llu,%llu,%llu,%llu (0,1-31,32-127,128-255,256-511,512+)\n",
        static_cast<unsigned long long>(empty_pairs.load()),
        static_cast<unsigned long long>(batch_sizes[0].load()),
        static_cast<unsigned long long>(batch_sizes[1].load()),
        static_cast<unsigned long long>(batch_sizes[2].load()),
        static_cast<unsigned long long>(batch_sizes[3].load()),
        static_cast<unsigned long long>(batch_sizes[4].load()),
        static_cast<unsigned long long>(batch_sizes[5].load()));
    fprintf(stderr, "[wgse-bwa-mem2] gpu_query_bins=%llu,%llu,%llu,%llu,%llu,%llu"
        " (1-8,9-16,17-32,33-64,65-128,129-256)\n",
        static_cast<unsigned long long>(query_sizes[0].load()),
        static_cast<unsigned long long>(query_sizes[1].load()),
        static_cast<unsigned long long>(query_sizes[2].load()),
        static_cast<unsigned long long>(query_sizes[3].load()),
        static_cast<unsigned long long>(query_sizes[4].load()),
        static_cast<unsigned long long>(query_sizes[5].load()));
}

bool wgse_metal_extend(SeqPair *pairs, const uint8_t *reference,
    const uint8_t *query, int32_t count, int32_t bandwidth,
    int32_t open_deletion, int32_t extend_deletion, int32_t open_insertion,
    int32_t extend_insertion, int32_t zdrop, int32_t end_bonus,
    const int8_t *matrix, int32_t precision)
{
    if (count == 0) return true;
    last_precision = precision;
    last_count = count;
#ifdef WGSE_METAL_BENCHMARK_TOOLS
    if (!capture_directory.empty()) {
        Parameters capture{count, bandwidth, open_deletion, extend_deletion,
            open_insertion, extend_insertion, zdrop, end_bonus, 0, {}, false,
            static_cast<uint8_t>(precision)};
        memcpy(capture.matrix, matrix, sizeof(capture.matrix));
        capture_batch(pairs, reference, query, capture);
    }
#endif
    Backend backend = selected_backend.load();
    bool hybrid = !force_gpu.load();
    if (backend == Backend::cpu)
        return fallback("CPU backend", false, CpuReason::selected);
    if (hybrid && precision == 8 && !enable_gpu8)
        return fallback("8-bit CPU policy", false, CpuReason::precision8);
    if (hybrid && count < (precision == 8 ? minimum_pairs8 : automatic_minimum_pairs))
        return fallback("small batch", false, CpuReason::small);
    if (count < 0 || count > max_pairs || bandwidth < 0 || bandwidth > 4096
        || open_deletion < 0 || open_deletion > 1000000
        || open_insertion < 0 || open_insertion > 1000000
        || extend_deletion <= 0 || extend_deletion > 100000
        || extend_insertion <= 0 || extend_insertion > 100000
        || end_bonus < -1000000 || end_bonus > 1000000)
        return fallback("unsupported extension parameters", true, CpuReason::semantics);

    bool simple_matrix = true;
    int32_t max_substitution = 0, min_substitution = 0;
    for (int i = 0; i < 5; ++i) {
        for (int j = 0; j < 5; ++j) {
            simple_matrix &= matrix[i * 5 + j]
                == (i == 4 || j == 4 ? matrix[24] : i == j ? matrix[0] : matrix[1]);
            max_substitution = std::max(max_substitution, int32_t(matrix[i * 5 + j]));
            min_substitution = std::min(min_substitution, int32_t(matrix[i * 5 + j]));
        }
    }
    // Upstream SIMD uses wrapping narrow arithmetic and a unit-gap Z-drop,
    // unlike its scalar oracle. Leave non-equivalent scoring settings on CPU.
    int32_t score_limit = precision == 8 ? 127 : 32767;
    if (precision != 0 && (!simple_matrix || extend_deletion != 1
        || extend_insertion != 1 || zdrop <= 0 || zdrop > score_limit
        // H + substitution - (open + extend) must not wrap below the lane minimum.
        || open_deletion + extend_deletion - min_substitution > score_limit
        || open_insertion + extend_insertion - min_substitution > score_limit))
        return fallback("unsupported SIMD scoring semantics", false, CpuReason::semantics);

    size_t reference_bytes = 0, query_bytes = 0;
    int32_t active_count = 0;
    uint64_t lengths[6]{};
    for (int32_t i = 0; i < count; ++i) {
        const auto &pair = pairs[i];
        if (pair.len1 < 0 || pair.len1 > max_target || pair.len2 < 0
            || pair.len2 > max_query || pair.h0 <= 0 || pair.h0 > 32767
            || pair.idr < 0 || pair.idq < 0)
            return fallback("unsupported extension dimensions len1="
                + std::to_string(pair.len1) + " len2=" + std::to_string(pair.len2)
                + " h0=" + std::to_string(pair.h0), true, CpuReason::dimensions);
        if (precision != 0 && pair.len1 != 0 && pair.len2 != 0) {
            int32_t insertion_band = pair.len2 * max_substitution + end_bonus - open_insertion;
            int32_t deletion_band = pair.len2 * max_substitution + end_bonus - open_deletion;
            if (pair.h0 + std::min(pair.len1, pair.len2) * max_substitution > score_limit
                || insertion_band < 0 || deletion_band < 0
                || insertion_band > score_limit || deletion_band > score_limit)
                return fallback("unsupported SIMD score or band range", false, CpuReason::semantics);
            int32_t width = std::min(bandwidth,
                std::max(1, std::min(insertion_band, deletion_band) + 1));
            if (width + 1 < pair.len2 && pair.h0 > open_insertion + width + 1)
                return fallback("SIMD narrow-band boundary differs from scalar", false, CpuReason::semantics);
        }
        if (pair.len1 != 0 && pair.len2 != 0) {
            ++active_count;
            int bin = pair.len2 <= 8 ? 0 : pair.len2 <= 16 ? 1 : pair.len2 <= 32 ? 2
                : pair.len2 <= 64 ? 3 : pair.len2 <= 128 ? 4 : 5;
            ++lengths[bin];
            reference_bytes += static_cast<size_t>(pair.len1);
            query_bytes += static_cast<size_t>(pair.len2);
        }
    }
    auto finish_empty = [&] {
        for (int32_t i = 0; i < count; ++i) {
            auto &pair = pairs[i];
            if (pair.len1 != 0 && pair.len2 != 0) continue;
            pair.score = pair.h0;
            pair.tle = pair.qle = pair.gtle = pair.max_off = 0;
            pair.gscore = -1;
        }
        empty_pairs += static_cast<uint64_t>(count - active_count);
        int bin = active_count == 0 ? 0 : active_count < 32 ? 1
            : active_count < 128 ? 2 : active_count < 256 ? 3 : active_count < 512 ? 4 : 5;
        ++batch_sizes[bin];
    };
    if (active_count == 0) {
        finish_empty();
        last_call_gpu = true;
        return true;
    }
    size_t pair_bytes = static_cast<size_t>(active_count) * sizeof(Job);
    if (reference_bytes + query_bytes + pair_bytes > max_bytes)
        return fallback("extension batch exceeds memory bound", true, CpuReason::dimensions);
    std::string unavailable;
    if (!wgse_metal_available(unavailable)) return fallback(unavailable, true);
    uint8_t largest = 0;
    for (int32_t i = 0; i < count; ++i) {
        const auto &pair = pairs[i];
        if (pair.len1 == 0 || pair.len2 == 0) continue;
        for (int j = 0; j < pair.len1; ++j)
            largest = std::max(largest, reference[pair.idr + j]);
        for (int j = 0; j < pair.len2; ++j)
            largest = std::max(largest, query[pair.idq + j]);
    }
    if (largest > 4)
        return fallback("base outside A/C/G/T/N encoding", true, CpuReason::dimensions);
    Parameters parameters{active_count, bandwidth, open_deletion, extend_deletion,
        open_insertion, extend_insertion, zdrop, end_bonus,
        max_substitution, {}, simple_matrix, static_cast<uint8_t>(precision)};
    memcpy(parameters.matrix, matrix, sizeof(parameters.matrix));
    Request request{pairs, reference, query, count, active_count,
        reference_bytes, query_bytes, parameters, nullptr, 0, false, {}, {}};
    if (!aggregator().extend(request, hybrid)) return false;
    finish_empty();
    for (int i = 0; i < 6; ++i) query_sizes[i] += lengths[i];
    last_call_gpu = true;
    return true;
}
