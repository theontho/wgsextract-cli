#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>

struct dnaSeqPair;

enum class Backend { automatic, cpu, metal };
enum class ExtensionKernel { row, striped, shared, tiled, adaptive };

bool wgse_metal_available(std::string &reason);
// False when the failure is missing or unsupported hardware rather than a
// broken backend (shader compilation, pipeline or queue creation).
bool wgse_metal_hardware_supported();
// True after a GPU execution failure that was recovered on the CPU.
bool wgse_metal_execution_failed(std::string &reason);
void wgse_set_backend(Backend backend);
void wgse_print_backend_metrics();
uint64_t wgse_gpu_execution_ns();
uint64_t wgse_gpu_dispatches();
uint64_t wgse_gpu_cells();
uint64_t wgse_gpu_issued_cells();
void wgse_set_kernel(ExtensionKernel kernel);
void wgse_set_routing(int32_t minimum_pairs, int32_t inflight_limit);
void wgse_set_aggregation(int32_t target, int32_t delay_us, int32_t queue_pairs);
void wgse_set_force_gpu(bool force);
void wgse_set_gpu8(bool enabled, int32_t target, int32_t minimum_pairs = 1,
    bool paired = true);
void wgse_set_pipelining(bool enabled);
// Whether the pipeline should map each chunk while the previous one pairs:
// with GPU mate rescue, pairing is mostly GPU work.
bool wgse_metal_overlap();
void wgse_set_overlap(bool enable);
// A chunk finished mapping while the previous one was pairing.
void wgse_note_overlapped_chunk();
// Page-aligned memory for the FM index checkpoints, so Metal can use them in place.
void *wgse_metal_index_alloc(size_t bytes);
// GPU SMEM collection (enabled with Metal) and its test hooks.
void wgse_set_gpu_seed(bool enable);
void wgse_set_gpu_seed_batch(size_t reads);
// SMEMs per read on the GPU before the CPU collects the read instead.
void wgse_set_gpu_seed_capacity(size_t smems);
void wgse_inject_seed_failure();
// Also collect each GPU unit's SMEMs on the CPU and count differences.
void wgse_verify_gpu_seeds();
// GPU mate rescue (enabled with Metal) and its test hooks.
void wgse_set_gpu_rescue(bool enable);
void wgse_set_gpu_rescue_batch(size_t pairs);
void wgse_inject_rescue_failure();
// Also run each GPU rescue alignment on the CPU and count differences.
void wgse_verify_gpu_rescue();
// Report the first commit of every nth piece of GPU work as interrupted.
void wgse_inject_gpu_interruptions(size_t every);
struct WgseExtensionScratch {
    dnaSeqPair *pairs, *aux;
    int32_t *hist;
};
WgseExtensionScratch wgse_extension_scratch(
    const dnaSeqPair *pairs, int32_t count, int32_t histogram_size);
struct WgseExtensionWork {
    explicit WgseExtensionWork(std::function<void(bool)> work);
    ~WgseExtensionWork();
    void finish(bool independent = false);
    std::function<void(bool)> work;
    WgseExtensionWork *previous;
    bool done = false;
};
#ifdef WGSE_METAL_BENCHMARK_TOOLS
bool wgse_set_capture_directory(const std::string &path, std::string &reason);
#endif
// Records wall time of one upstream batch-extension call, whichever backend ran it.
struct WgseExtensionScope {
    explicit WgseExtensionScope(int32_t count);
    ~WgseExtensionScope();
    uint64_t start;
    int32_t count;
    uint64_t children = 0;
    WgseExtensionScope *parent;
};
bool wgse_metal_extend(dnaSeqPair *pairs, const uint8_t *reference,
    const uint8_t *query, int32_t count, int32_t bandwidth,
    int32_t open_deletion, int32_t extend_deletion, int32_t open_insertion,
    int32_t extend_insertion, int32_t zdrop, int32_t end_bonus,
    const int8_t *matrix, int32_t precision = 0);
