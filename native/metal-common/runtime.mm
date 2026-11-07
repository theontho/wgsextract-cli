#include "runtime.h"

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <limits>
#include <unistd.h>

namespace wgse::metal {

namespace {
std::atomic<size_t> aborted{0}, first_commits{0}, injected_every{0};
constexpr long gpu_slots = 2;
dispatch_semaphore_t gpu_slot() {
    static dispatch_semaphore_t slot = dispatch_semaphore_create(gpu_slots);
    return slot;
}
} // namespace

void Runtime::commit(id<MTLCommandBuffer> command) {
    dispatch_semaphore_t slot = gpu_slot();
    dispatch_semaphore_wait(slot, DISPATCH_TIME_FOREVER);
    [command addCompletedHandler:^(id<MTLCommandBuffer>) { dispatch_semaphore_signal(slot); }];
    [command commit];
}

bool Runtime::interrupted(id<MTLCommandBuffer> command, int attempt) {
    const size_t every = injected_every.load(std::memory_order_relaxed);
    if (every && attempt == 1 && first_commits.fetch_add(1) % every == every - 1) {
        aborted.fetch_add(1, std::memory_order_relaxed);
        return true;
    }
    NSError *error = command.status == MTLCommandBufferStatusError ? command.error : nil;
    NSError *cause = error.userInfo[NSUnderlyingErrorKey];
    // kIOGPUCommandBufferCallbackErrorImpactingInteractivity
    if (![error.domain isEqualToString:MTLCommandBufferErrorDomain]
        || ![cause.domain isEqualToString:@"IOGPUCommandQueueErrorDomain"] || cause.code != 14)
        return false;
    aborted.fetch_add(1, std::memory_order_relaxed);
    return true;
}

void Runtime::inject_interruptions(size_t every) {
    injected_every = every;
}

size_t Runtime::interruptions() {
    return aborted.load(std::memory_order_relaxed);
}

std::string Runtime::failure() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return failure_;
}

void Runtime::fail(const std::string &reason) {
    std::lock_guard<std::mutex> lock(mutex_);
    failure_ = reason;
}

Runtime::Runtime() {
    device_ = MTLCreateSystemDefaultDevice();
    if (!device_ || !device_.hasUnifiedMemory) {
        fail("no unified-memory Metal device");
        device_ = nil;
        return;
    }
    queue_ = [device_ newCommandQueue];
    if (!queue_) fail("Metal command queue allocation failed");
}

id<MTLComputePipelineState> Runtime::compile(const char *source, const char *entry) {
    return pipeline(library(source), entry);
}

id<MTLLibrary> Runtime::library(const char *source) {
    if (failed()) return nil;
    NSError *error = nil;
    id<MTLLibrary> library = [device_ newLibraryWithSource:
        [NSString stringWithUTF8String:source] options:nil error:&error];
    if (!library) fail(error.localizedDescription.UTF8String);
    return library;
}

id<MTLComputePipelineState> Runtime::pipeline(id<MTLLibrary> library, const char *entry,
    MTLFunctionConstantValues *constants) {
    if (failed() || !library) return nil;
    NSError *error = nil;
    NSString *name = [NSString stringWithUTF8String:entry];
    id<MTLFunction> function = constants
        ? [library newFunctionWithName:name constantValues:constants error:&error]
        : [library newFunctionWithName:name];
    if (!function) {
        std::string reason = std::string("Metal kernel missing: ") + entry;
        if (error) reason += std::string(": ") + error.localizedDescription.UTF8String;
        fail(reason);
        return nil;
    }
    // The label names the kernel's command buffers in GPU traces.
    MTLComputePipelineDescriptor *descriptor = [MTLComputePipelineDescriptor new];
    descriptor.computeFunction = function;
    descriptor.label = function.name;
    id<MTLComputePipelineState> pipeline = [device_ newComputePipelineStateWithDescriptor:descriptor
        options:MTLPipelineOptionNone reflection:nil error:&error];
    if (!pipeline) fail(error.localizedDescription.UTF8String);
    return pipeline;
}

id<MTLBuffer> Runtime::allocate(size_t bytes) {
    if (!device_ || bytes == 0 || bytes > device_.maxBufferLength) return nil;
    return [device_ newBufferWithLength:bytes options:MTLResourceStorageModeShared];
}

id<MTLBuffer> Runtime::allocate_pages(size_t bytes) {
    const size_t page = static_cast<size_t>(getpagesize());
    if (!device_ || bytes == 0 || bytes > std::numeric_limits<size_t>::max() - page)
        return nil;
    size_t capacity = (bytes + page - 1) / page * page;
    if (capacity > device_.maxBufferLength) return nil;
    void *storage = nullptr;
    if (posix_memalign(&storage, page, capacity) != 0) return nil;
    id<MTLBuffer> buffer = [device_ newBufferWithBytesNoCopy:storage length:capacity
        options:MTLResourceStorageModeShared
        deallocator:^(void *pointer, NSUInteger) { free(pointer); }];
    if (!buffer) free(storage);
    return buffer;
}

bool Runtime::dispatch(id<MTLComputePipelineState> pipeline,
    const std::vector<id<MTLBuffer>> &buffers, size_t count, size_t group_size,
    double &gpu_seconds) {
    gpu_seconds = 0;
    if (!pipeline || !queue_ || group_size == 0 ||
        group_size > pipeline.maxTotalThreadsPerThreadgroup) {
        fail("invalid Metal dispatch");
        return false;
    }
    if (count == 0) return true;
    for (size_t i = 0; i < buffers.size(); ++i) {
        if (!buffers[i]) {
            fail("missing Metal dispatch buffer");
            return false;
        }
    }
    for (int attempt = 1;; ++attempt) {
        id<MTLCommandBuffer> command = [queue_ commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
        if (!command || !encoder) {
            fail("Metal command allocation failed");
            return false;
        }
        command.label = encoder.label = pipeline.label;
        [encoder setComputePipelineState:pipeline];
        for (size_t i = 0; i < buffers.size(); ++i)
            [encoder setBuffer:buffers[i] offset:0 atIndex:i];
        [encoder dispatchThreads:MTLSizeMake(count, 1, 1)
            threadsPerThreadgroup:MTLSizeMake(group_size, 1, 1)];
        [encoder endEncoding];
        commit(command);
        [command waitUntilCompleted];
        if (attempt < attempts && interrupted(command, attempt)) continue;
        if (command.status == MTLCommandBufferStatusCompleted) {
            gpu_seconds = std::max(0.0, command.GPUEndTime - command.GPUStartTime);
            return true;
        }
        fail(command.error ? command.error.localizedDescription.UTF8String
                           : "Metal command failed");
        return false;
    }
}

} // namespace wgse::metal
