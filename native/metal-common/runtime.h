#pragma once

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <cstddef>
#include <mutex>
#include <string>
#include <vector>

namespace wgse::metal {

// Objective-C++ interface: independent of either aligner's index/read formats.
class Runtime {
public:
    Runtime();
    id<MTLDevice> device() const { return device_; }
    // For callers that encode their own command buffers.
    id<MTLCommandQueue> queue() const { return queue_; }
    // The first failure, if any; threads may use the runtime concurrently.
    std::string failure() const;
    id<MTLComputePipelineState> compile(const char *source, const char *entry);
    // For kernels specialised by function constants: one library, several pipelines.
    id<MTLLibrary> library(const char *source);
    id<MTLComputePipelineState> pipeline(id<MTLLibrary> library, const char *entry,
        MTLFunctionConstantValues *constants = nil);
    id<MTLBuffer> allocate(size_t bytes);
    // Page-aligned storage owned by the buffer; its contents are CPU/GPU shared.
    id<MTLBuffer> allocate_pages(size_t bytes);
    // Commits command once fewer than gpu_slots command buffers of this process
    // are running. Every command buffer goes through here: with more running at
    // once, macOS aborts some (ImpactingInteractivity) to keep the display responsive.
    static void commit(id<MTLCommandBuffer> command);
    // Whether macOS aborted command, the attempt'th commit of its work, to keep
    // the display responsive (ImpactingInteractivity), for example while another
    // process uses the GPU. Its kernels ran partly or not at all; committing them
    // again is safe when they only write outputs derived from unchanged inputs.
    // Counts the aborts.
    static bool interrupted(id<MTLCommandBuffer> command, int attempt);
    static size_t interruptions();
    // Commits of one piece of work, including those after interruptions.
    static constexpr int attempts = 4;
    // Test hook: report the first commit of every nth piece of work as
    // interrupted even if it completed, so its kernels run twice.
    static void inject_interruptions(size_t every);
    bool dispatch(id<MTLComputePipelineState> pipeline,
        const std::vector<id<MTLBuffer>> &buffers, size_t count, size_t group_size,
        double &gpu_seconds);

private:
    id<MTLDevice> device_ = nil;
    id<MTLCommandQueue> queue_ = nil;
    mutable std::mutex mutex_;
    std::string failure_;
    bool failed() const { return !failure().empty(); }
    void fail(const std::string &reason);
};

} // namespace wgse::metal
