#pragma once

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include <cstddef>
#include <string>
#include <vector>

namespace wgse::metal {

// Objective-C++ interface: independent of either aligner's index/read formats.
class Runtime {
public:
    Runtime();
    id<MTLDevice> device() const { return device_; }
    const std::string &failure() const { return failure_; }
    id<MTLComputePipelineState> compile(const char *source, const char *entry);
    id<MTLBuffer> allocate(size_t bytes);
    // Page-aligned storage owned by the buffer; its contents are CPU/GPU shared.
    id<MTLBuffer> allocate_pages(size_t bytes);
    bool dispatch(id<MTLComputePipelineState> pipeline,
        const std::vector<id<MTLBuffer>> &buffers, size_t count, size_t group_size,
        double &gpu_seconds);

private:
    id<MTLDevice> device_ = nil;
    id<MTLCommandQueue> queue_ = nil;
    std::string failure_;
};

} // namespace wgse::metal
