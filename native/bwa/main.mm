#include "metal_backend.h"
#include "seeding_types.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace {
constexpr const char *name = "wgse-bwa";
constexpr const char *version = WGSE_VERSION;
constexpr const char *engine_version = "0.7.19";

void help() {
    printf("%s %s (BWA %s rebuilt for Apple Silicon)\n"
        "Usage: %s [--metal | --backend cpu|metal|auto] <command> [arguments]\n"
        "  <command>                 a BWA command such as index or mem (run %s alone to list them)\n"
        "  capabilities --json       report Metal availability\n"
        "  --version                 print executable and engine version\n"
        "Options precede the command and affect only mem. The default CPU backend is unmodified BWA.\n"
        "--metal (EXPERIMENTAL) finds and locates the seeds of most reads, extends most\n"
        "seeds and aligns many mate-rescue windows on the GPU while the CPU does the\n"
        "rest, with identical output. It fails if Metal is\n"
        "unavailable, and exits 1 after finishing on the CPU if a GPU batch fails;\n"
        "--backend auto falls back to the CPU in both cases.\n"
        "SAM is written to stdout; diagnostics to stderr.\n"
        "Not qualified for clinical use.\n",
        name, version, engine_version, name, name);
}

bool parse_backend(const char *value, Backend &backend) {
    if (strcmp(value, "cpu") == 0) backend = Backend::cpu;
    else if (strcmp(value, "metal") == 0) backend = Backend::metal;
    else if (strcmp(value, "auto") == 0) backend = Backend::automatic;
    else return false;
    return true;
}

int run(int argc, char **argv) {
    int command_index = 1;
    Backend backend = Backend::cpu;
    while (argc > command_index) {
        const char *option = argv[command_index];
        if (strcmp(option, "--metal") == 0) {
            backend = Backend::metal;
            ++command_index;
        } else if (strcmp(option, "--backend") == 0) {
            if (argc <= command_index + 1 || !parse_backend(argv[command_index + 1], backend)) {
                fprintf(stderr, "--backend requires cpu, metal or auto\n");
                return 2;
            }
            command_index += 2;
#ifdef WGSE_METAL_BENCHMARK_TOOLS
        } else if (strcmp(option, "--gpu-inject-failure") == 0) {
            wgse_inject_gpu_failure();
            ++command_index;
        } else if (strcmp(option, "--gpu-inject-locate-failure") == 0) {
            wgse_inject_locate_failure();
            ++command_index;
        } else if (strcmp(option, "--gpu-inject-interruptions") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long long every = strtoll(value, &end, 10);
            if (end == value || *end || every < 1) {
                fprintf(stderr, "--gpu-inject-interruptions requires a positive count\n");
                return 2;
            }
            wgse_inject_gpu_interruptions(static_cast<size_t>(every));
            command_index += 2;
        } else if (strcmp(option, "--gpu-locate") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            if (strcmp(value, "on") && strcmp(value, "off")) {
                fprintf(stderr, "--gpu-locate requires on or off\n");
                return 2;
            }
            wgse_set_gpu_locate(strcmp(value, "on") == 0);
            command_index += 2;
        } else if (strcmp(option, "--gpu-locate-dense") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long shift = strtol(value, &end, 10);
            if (end == value || *end || shift < 0 || shift > 4) {
                fprintf(stderr, "--gpu-locate-dense requires a sample shift of 0..4\n");
                return 2;
            }
            wgse_set_gpu_locate_dense(static_cast<unsigned>(shift));
            command_index += 2;
        } else if (strcmp(option, "--gpu-locate-dense-after") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long long reads = strtoll(value, &end, 10);
            if (end == value || *end || reads < 0) {
                fprintf(stderr, "--gpu-locate-dense-after requires a read count\n");
                return 2;
            }
            wgse_set_gpu_locate_dense_after(static_cast<size_t>(reads));
            command_index += 2;
        } else if (strcmp(option, "--gpu-locate-dense-slice") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long long entries = strtoll(value, &end, 10);
            if (end == value || *end || entries <= 0 || entries % 32 || entries > (1LL << 30)) {
                fprintf(stderr, "--gpu-locate-dense-slice requires a positive multiple of 32\n");
                return 2;
            }
            wgse_set_gpu_locate_dense_slice(static_cast<size_t>(entries));
            command_index += 2;
        } else if (strcmp(option, "--gpu-policy") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            if (strcmp(value, "all") && strcmp(value, "hybrid")) {
                fprintf(stderr, "--gpu-policy requires all or hybrid\n");
                return 2;
            }
            wgse_set_gpu_policy(strcmp(value, "all") == 0);
            command_index += 2;
        } else if (strcmp(option, "--gpu-batch") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long reads = strtol(value, &end, 10);
            if (end == value || *end || reads < 2 || reads > 65536) {
                fprintf(stderr, "--gpu-batch requires 2..65536 reads\n");
                return 2;
            }
            wgse_set_gpu_batch(static_cast<size_t>(reads));
            command_index += 2;
        } else if (strcmp(option, "--gpu-locate-rows") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long rows = strtol(value, &end, 10);
            if (end == value || *end || rows < 0 || rows > (1L << 21)) {
                fprintf(stderr, "--gpu-locate-rows requires 0..2097152 rows\n");
                return 2;
            }
            wgse_set_gpu_locate_rows(static_cast<size_t>(rows));
            command_index += 2;
        } else if (strcmp(option, "--gpu-rescue") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            if (strcmp(value, "on") && strcmp(value, "off")) {
                fprintf(stderr, "--gpu-rescue requires on or off\n");
                return 2;
            }
            wgse_set_gpu_rescue(strcmp(value, "on") == 0);
            command_index += 2;
        } else if (strcmp(option, "--gpu-rescue-batch") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long pairs = strtol(value, &end, 10);
            if (end == value || *end || pairs < 1 || pairs > 65536) {
                fprintf(stderr, "--gpu-rescue-batch requires 1..65536 pairs\n");
                return 2;
            }
            wgse_set_gpu_rescue_batch(static_cast<size_t>(pairs));
            command_index += 2;
        } else if (strcmp(option, "--gpu-inject-rescue-failure") == 0) {
            wgse_inject_rescue_failure();
            ++command_index;
        } else if (strcmp(option, "--gpu-rescue-verify") == 0) {
            wgse_verify_gpu_rescue();
            ++command_index;
        } else if (strcmp(option, "--gpu-extend") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            if (strcmp(value, "on") && strcmp(value, "off")) {
                fprintf(stderr, "--gpu-extend requires on or off\n");
                return 2;
            }
            wgse_set_gpu_extend(strcmp(value, "on") == 0);
            command_index += 2;
        } else if (strcmp(option, "--gpu-extend-pairs") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long pairs = strtol(value, &end, 10);
            if (end == value || *end || pairs < 1 || pairs > 4096) {
                fprintf(stderr, "--gpu-extend-pairs requires 1..4096 pairs\n");
                return 2;
            }
            wgse_set_gpu_extend_pairs(static_cast<size_t>(pairs));
            command_index += 2;
        } else if (strcmp(option, "--gpu-extend-batches") == 0) {
            const char *value = argc > command_index + 1 ? argv[command_index + 1] : "";
            char *end = nullptr;
            long batches = strtol(value, &end, 10);
            if (end == value || *end || batches < 1 || batches > 64) {
                fprintf(stderr, "--gpu-extend-batches requires 1..64 batches\n");
                return 2;
            }
            wgse_set_gpu_extend_batches(static_cast<size_t>(batches));
            command_index += 2;
        } else if (strcmp(option, "--gpu-inject-extend-failure") == 0) {
            wgse_inject_extend_failure();
            ++command_index;
        } else if (strcmp(option, "--gpu-extend-verify") == 0) {
            wgse_verify_gpu_extend();
            ++command_index;
#endif
        } else break;
    }
#ifdef WGSE_METAL_BENCHMARK_TOOLS
    if (wgse_gpu_extend_pool_pairs() > 16384) {
        fprintf(stderr, "--gpu-extend-pairs times --gpu-extend-batches must not exceed 16384\n");
        return 2;
    }
#endif
    const std::string command = argc > command_index ? argv[command_index] : "";
    if (command == "--help" || command == "-h") {
        help();
        return 0;
    }
    if (command == "--version" || command == "version") {
        printf("%s %s; BWA %s\n", name, version, engine_version);
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
            "\"engine\":\"bwa\",\"engine_version\":\"%s\","
            "\"qualified\":false,\"metal_available\":%s,"
            "\"metal_hardware_supported\":%s,"
            "\"cpu_available\":true,\"max_gpu_query_length\":%u}\n",
            version, engine_version, available ? "true" : "false",
            wgse_metal_hardware_supported() ? "true" : "false",
            static_cast<unsigned>(seed_max_length));
        return 0;
    }
    if (command_index == 1) return wgse_bwa_main(argc, argv);
    if (command.empty()) {
        help();
        return 2;
    }
    const bool accelerated = command == "mem" && backend != Backend::cpu;
    if (accelerated && backend == Backend::metal) {
        std::string reason;
        if (!wgse_metal_available(reason)) {
            fprintf(stderr, "Metal unavailable: %s\n", reason.c_str());
            return 1;
        }
    }
    if (accelerated) wgse_set_backend(backend);
    // Upstream records argv[0] plus its arguments as the @PG CL and stderr CMD.
    // Fold the wrapper options into argv[0] so both keep the full command line.
    std::string program = argv[0];
    for (int i = 1; i < command_index; ++i) program.append(" ").append(argv[i]);
    std::vector<char *> arguments{program.data()};
    arguments.insert(arguments.end(), argv + command_index, argv + argc);
    arguments.push_back(nullptr);
    int result = wgse_bwa_main(static_cast<int>(arguments.size() - 1), arguments.data());
    if (!accelerated) return result;
    wgse_finish_backend();
    std::string failure;
    if (backend == Backend::metal && wgse_metal_execution_failed(failure)) {
        fprintf(stderr, "[%s] Metal execution failed (alignments were completed on the CPU): %s\n",
            name, failure.c_str());
        if (result == 0) result = 1;
    }
    return result;
}
} // namespace

int main(int argc, char **argv) {
    @autoreleasepool {
        return run(argc, argv);
    }
}
