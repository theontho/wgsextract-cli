#include "runtime.h"

#include <algorithm>
#include <cstdlib>
#include <limits>
#include <unistd.h>

namespace wgse::metal {

Runtime::Runtime() {
    device_ = MTLCreateSystemDefaultDevice();
    if (!device_ || !device_.hasUnifiedMemory) {
        failure_ = "no unified-memory Metal device";
        device_ = nil;
        return;
    }
    queue_ = [device_ newCommandQueue];
    if (!queue_) failure_ = "Metal command queue allocation failed";
}

id<MTLComputePipelineState> Runtime::compile(const char *source, const char *entry) {
    if (!failure_.empty()) return nil;
    NSError *error = nil;
    id<MTLLibrary> library = [device_ newLibraryWithSource:
        [NSString stringWithUTF8String:source] options:nil error:&error];
    if (!library) {
        failure_ = error.localizedDescription.UTF8String;
        return nil;
    }
    id<MTLFunction> function = [library newFunctionWithName:
        [NSString stringWithUTF8String:entry]];
    if (!function) {
        failure_ = std::string("Metal kernel missing: ") + entry;
        return nil;
    }
    id<MTLComputePipelineState> pipeline =
        [device_ newComputePipelineStateWithFunction:function error:&error];
    if (!pipeline) failure_ = error.localizedDescription.UTF8String;
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
        failure_ = "invalid Metal dispatch";
        return false;
    }
    if (count == 0) return true;
    id<MTLCommandBuffer> command = [queue_ commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
    if (!command || !encoder) {
        failure_ = "Metal command allocation failed";
        return false;
    }
    [encoder setComputePipelineState:pipeline];
    for (size_t i = 0; i < buffers.size(); ++i) {
        if (!buffers[i]) {
            [encoder endEncoding];
            failure_ = "missing Metal dispatch buffer";
            return false;
        }
        [encoder setBuffer:buffers[i] offset:0 atIndex:i];
    }
    [encoder dispatchThreads:MTLSizeMake(count, 1, 1)
        threadsPerThreadgroup:MTLSizeMake(group_size, 1, 1)];
    [encoder endEncoding];
    [command commit];
    [command waitUntilCompleted];
    if (command.status != MTLCommandBufferStatusCompleted) {
        failure_ = command.error ? command.error.localizedDescription.UTF8String
                                 : "Metal command failed";
        return false;
    }
    gpu_seconds = std::max(0.0, command.GPUEndTime - command.GPUStartTime);
    return true;
}

} // namespace wgse::metal
