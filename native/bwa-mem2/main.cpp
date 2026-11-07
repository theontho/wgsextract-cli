#include "main.h"
#include "metal_backend.h"

#include <string>

uint64_t proc_freq, tprof[LIM_R][LIM_C], prof[LIM_R];

namespace {
constexpr const char *name = "wgse-bwa-mem2";
constexpr const char *version = WGSE_VERSION;

void help() {
    printf("%s %s (BWA-MEM2 2.3 rebuilt for Apple Silicon)\n"
        "Usage: %s [--metal | --backend cpu|metal|auto] <command> [arguments]\n"
        "  index <reference.fa>      build a BWA-MEM2 index\n"
        "  mem [BWA options] <index> <R1.fastq[.gz]> [R2.fastq[.gz]]\n"
        "  capabilities --json       report Metal availability\n"
        "  --version                 print executable and engine version\n"
        "Options precede the command. The default CPU backend uses BWA-MEM2's own extension code.\n"
        "--metal (EXPERIMENTAL) offloads SMEM seeding, batched seed extension and\n"
        "paired-end mate rescue to the GPU. It fails if Metal is unavailable, and exits 1 after finishing\n"
        "on the CPU if a GPU batch fails; --backend auto falls back to the CPU in both cases.\n"
        "SAM is written to stdout; diagnostics to stderr.\n"
        "Not qualified for clinical use.\n",
        name, version, name);
}

bool parse_backend(const char *value, Backend &backend) {
    if (strcmp(value, "cpu") == 0) backend = Backend::cpu;
    else if (strcmp(value, "metal") == 0) backend = Backend::metal;
    else if (strcmp(value, "auto") == 0) backend = Backend::automatic;
    else return false;
    return true;
}
}

int main(int argc, char **argv) {
    uint64_t frequency;
    __asm__ volatile("mrs %0, cntfrq_el0" : "=r"(frequency));
    proc_freq = frequency;
    int command_index = 1;
    Backend backend = Backend::cpu;
    while (argc > command_index) {
        if (strcmp(argv[command_index], "--metal") == 0) {
            backend = Backend::metal;
            ++command_index;
        } else if (strcmp(argv[command_index], "--backend") == 0) {
            if (argc <= command_index + 1 || !parse_backend(argv[command_index + 1], backend)) {
                fprintf(stderr, "--backend requires cpu, metal or auto\n");
                return 2;
            }
            command_index += 2;
        } else break;
    }
#ifdef WGSE_METAL_BENCHMARK_TOOLS
    int gpu_minimum = 128, gpu_inflight = 16;
    int gpu_target = 8192, gpu_delay = 100, gpu_queue = 32768;
    int gpu_target8 = 8192, gpu_min8 = 1;
    bool gpu8 = true;
    bool gpu8_paired = true;
    while (argc > command_index && (strcmp(argv[command_index], "--capture-batches") == 0
        || strcmp(argv[command_index], "--gpu-rescue") == 0
        || strcmp(argv[command_index], "--gpu-seed") == 0
        || strcmp(argv[command_index], "--gpu-seed-batch") == 0
        || strcmp(argv[command_index], "--gpu-seed-capacity") == 0
        || strcmp(argv[command_index], "--gpu-seed-verify") == 0
        || strcmp(argv[command_index], "--gpu-inject-seed-failure") == 0
        || strcmp(argv[command_index], "--gpu-overlap") == 0
        || strcmp(argv[command_index], "--gpu-rescue-batch") == 0
        || strcmp(argv[command_index], "--gpu-rescue-verify") == 0
        || strcmp(argv[command_index], "--gpu-inject-rescue-failure") == 0
        || strcmp(argv[command_index], "--gpu-inject-interruptions") == 0
        || strcmp(argv[command_index], "--gpu-policy") == 0
        || strcmp(argv[command_index], "--gpu-8bit") == 0
        || strcmp(argv[command_index], "--gpu-pipeline") == 0
        || strcmp(argv[command_index], "--gpu-target8-pairs") == 0
        || strcmp(argv[command_index], "--gpu-min8-pairs") == 0
        || strcmp(argv[command_index], "--gpu-kernel") == 0
        || strcmp(argv[command_index], "--gpu-inflight") == 0
        || strcmp(argv[command_index], "--gpu-target-pairs") == 0
        || strcmp(argv[command_index], "--gpu-delay-us") == 0
        || strcmp(argv[command_index], "--gpu-queue-pairs") == 0
        || strcmp(argv[command_index], "--gpu-min-pairs") == 0)) {
        std::string reason;
        if (strcmp(argv[command_index], "--gpu-rescue-verify") == 0
            || strcmp(argv[command_index], "--gpu-inject-rescue-failure") == 0
            || strcmp(argv[command_index], "--gpu-seed-verify") == 0
            || strcmp(argv[command_index], "--gpu-inject-seed-failure") == 0) {
            if (strcmp(argv[command_index], "--gpu-rescue-verify") == 0) wgse_verify_gpu_rescue();
            else if (strcmp(argv[command_index], "--gpu-inject-rescue-failure") == 0)
                wgse_inject_rescue_failure();
            else if (strcmp(argv[command_index], "--gpu-seed-verify") == 0) wgse_verify_gpu_seeds();
            else wgse_inject_seed_failure();
            ++command_index;
            continue;
        }
        if (argc <= command_index + 2) return 2;
        if (strcmp(argv[command_index], "--gpu-rescue") == 0
            || strcmp(argv[command_index], "--gpu-seed") == 0
            || strcmp(argv[command_index], "--gpu-overlap") == 0) {
            std::string value = argv[command_index + 1];
            if (value != "on" && value != "off") return 2;
            if (strcmp(argv[command_index], "--gpu-rescue") == 0) wgse_set_gpu_rescue(value == "on");
            else if (strcmp(argv[command_index], "--gpu-seed") == 0) wgse_set_gpu_seed(value == "on");
            else wgse_set_overlap(value == "on");
        } else if (strcmp(argv[command_index], "--gpu-rescue-batch") == 0
            || strcmp(argv[command_index], "--gpu-seed-batch") == 0
            || strcmp(argv[command_index], "--gpu-seed-capacity") == 0
            || strcmp(argv[command_index], "--gpu-inject-interruptions") == 0) {
            char *end = nullptr;
            long value = strtol(argv[command_index + 1], &end, 10);
            if (!end || end == argv[command_index + 1] || *end != '\0' || value < 1
                || value > 1000000) return 2;
            if (strcmp(argv[command_index], "--gpu-rescue-batch") == 0)
                wgse_set_gpu_rescue_batch(size_t(value));
            else if (strcmp(argv[command_index], "--gpu-seed-batch") == 0)
                wgse_set_gpu_seed_batch(size_t(value));
            else if (strcmp(argv[command_index], "--gpu-seed-capacity") == 0)
                wgse_set_gpu_seed_capacity(size_t(value));
            else wgse_inject_gpu_interruptions(size_t(value));
        } else if (strcmp(argv[command_index], "--gpu-pipeline") == 0) {
            std::string value = argv[command_index + 1];
            if (value != "on" && value != "off") return 2;
            wgse_set_pipelining(value == "on");
        } else if (strcmp(argv[command_index], "--gpu-8bit") == 0) {
            std::string value = argv[command_index + 1];
            if (value != "cpu" && value != "gpu" && value != "paired") return 2;
            gpu8 = value != "cpu";
            gpu8_paired = value == "paired";
            wgse_set_gpu8(gpu8, gpu_target8, gpu_min8, gpu8_paired);
        } else if (strcmp(argv[command_index], "--gpu-policy") == 0) {
            std::string value = argv[command_index + 1];
            if (value != "all" && value != "hybrid") return 2;
            wgse_set_force_gpu(value == "all");
        } else if (strcmp(argv[command_index], "--gpu-kernel") == 0) {
            std::string value = argv[command_index + 1];
            if (value != "row" && value != "striped" && value != "shared"
                && value != "tiled" && value != "adaptive")
                return 2;
            wgse_set_kernel(value == "row" ? ExtensionKernel::row
                : value == "striped" ? ExtensionKernel::striped
                : value == "shared" ? ExtensionKernel::shared
                : value == "tiled" ? ExtensionKernel::tiled : ExtensionKernel::adaptive);
        } else if (strcmp(argv[command_index], "--gpu-target-pairs") == 0
            || strcmp(argv[command_index], "--gpu-target8-pairs") == 0
            || strcmp(argv[command_index], "--gpu-min8-pairs") == 0
            || strcmp(argv[command_index], "--gpu-delay-us") == 0
            || strcmp(argv[command_index], "--gpu-queue-pairs") == 0) {
            char *end = nullptr;
            long value = strtol(argv[command_index + 1], &end, 10);
            if (!end || end == argv[command_index + 1] || *end != '\0'
                || value < 0 || value > 65536) return 2;
            if (strcmp(argv[command_index], "--gpu-delay-us") == 0)
                gpu_delay = static_cast<int>(value);
            else {
                if (value == 0) return 2;
                if (strcmp(argv[command_index], "--gpu-min8-pairs") == 0)
                    gpu_min8 = static_cast<int>(value);
                else if (strcmp(argv[command_index], "--gpu-target8-pairs") == 0)
                    gpu_target8 = static_cast<int>(value);
                else if (strcmp(argv[command_index], "--gpu-target-pairs") == 0)
                    gpu_target = static_cast<int>(value);
                else gpu_queue = static_cast<int>(value);
            }
            wgse_set_aggregation(gpu_target, gpu_delay, gpu_queue);
            wgse_set_gpu8(gpu8, gpu_target8, gpu_min8, gpu8_paired);
        } else if (strcmp(argv[command_index], "--gpu-inflight") == 0
            || strcmp(argv[command_index], "--gpu-min-pairs") == 0) {
            char *end = nullptr;
            long value = strtol(argv[command_index + 1], &end, 10);
            if (!end || end == argv[command_index + 1]
                || *end != '\0' || value < 1 || value > 65536) return 2;
            if (strcmp(argv[command_index], "--gpu-inflight") == 0) {
                if (value > 32) return 2;
                gpu_inflight = static_cast<int>(value);
            } else gpu_minimum = static_cast<int>(value);
            wgse_set_routing(gpu_minimum, gpu_inflight);
        } else if (!wgse_set_capture_directory(argv[command_index + 1], reason)) {
            fprintf(stderr, "Cannot capture extension batches: %s\n", reason.c_str());
            return 2;
        }
        command_index += 2;
    }
#endif
    if (argc <= command_index) {
        help();
        return 2;
    }
    const std::string command = argv[command_index];
    if (command == "--help" || command == "-h") {
        help();
        return 0;
    }
    if (command == "--version" || command == "version") {
        printf("%s %s; BWA-MEM2 2.3\n", name, version);
        return 0;
    }
    if (command == "capabilities") {
        if (argc != command_index + 2 || strcmp(argv[command_index + 1], "--json")) {
            fprintf(stderr, "Usage: %s capabilities --json\n", name);
            return 2;
        }
        std::string reason;
        bool available = wgse_metal_available(reason);
        if (!available) fprintf(stderr, "Metal unavailable: %s\n", reason.c_str());
        printf("{\"schema_version\":1,\"version\":\"%s\","
            "\"engine\":\"bwa-mem2\",\"engine_version\":\"2.3\","
            "\"qualified\":false,\"metal_available\":%s,"
            "\"metal_hardware_supported\":%s,"
            "\"cpu_available\":true,\"max_gpu_query_length\":256}\n",
            version, available ? "true" : "false",
            wgse_metal_hardware_supported() ? "true" : "false");
        return 0;
    }
    if (command == "index")
        return bwa_index(argc - command_index, argv + command_index);
    if (command != "mem") {
        fprintf(stderr, "Unknown command: %s\n", command.c_str());
        return 2;
    }
    wgse_set_backend(backend);
    if (backend == Backend::metal) {
        std::string reason;
        if (!wgse_metal_available(reason)) {
            fprintf(stderr, "Metal unavailable: %s\n", reason.c_str());
            return 1;
        }
    }
    extern char *bwa_pg;
    kstring_t pg{0, 0, nullptr};
    ksprintf(&pg, "@PG\tID:bwa-mem2\tPN:bwa-mem2\tVN:2.3-wgse-%s\tCL:%s", version, argv[0]);
    for (int i = 1; i < argc; ++i) ksprintf(&pg, " %s", argv[i]);
    ksprintf(&pg, "\n");
    bwa_pg = pg.s;
    tprof[MEM][0] = __rdtsc();
    int result = main_mem(argc - command_index, argv + command_index);
    free(bwa_pg);
    bwa_pg = nullptr;
    if (fflush(stdout) != 0 || ferror(stdout)) {
        fprintf(stderr, "[%s] failed to write SAM output\n", name);
        result = 1;
    }
    if (backend != Backend::cpu) wgse_print_backend_metrics();
    std::string failure;
    if (backend == Backend::metal && wgse_metal_execution_failed(failure)) {
        fprintf(stderr, "[%s] Metal execution failed (alignments were completed on the CPU): %s\n",
            name, failure.c_str());
        if (result == 0) result = 1;
    }
    return result;
}
