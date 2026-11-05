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
        "--metal (EXPERIMENTAL) seeds most reads on the GPU while the CPU maps the rest,\n"
        "with identical output. It fails if Metal is unavailable, and exits 1 after\n"
        "finishing on the CPU if a GPU batch fails; --backend auto falls back to the CPU\n"
        "in both cases.\n"
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
        } else if (strcmp(option, "--gpu-percent") == 0 || strcmp(option, "--gpu-policy") == 0) {
            if (argc <= command_index + 1) return 2;
            const char *value = argv[command_index + 1];
            char *end = nullptr;
            long percent = strcmp(value, "all") == 0 ? 100 : strcmp(value, "hybrid") == 0 ? 80
                : strcmp(option, "--gpu-percent") == 0 ? strtol(value, &end, 10) : 0;
            if ((end && (end == value || *end)) || percent < 1 || percent > 100) {
                fprintf(stderr, "%s requires 1..100, all or hybrid\n", option);
                return 2;
            }
            wgse_set_gpu_percent(static_cast<int>(percent));
            command_index += 2;
#endif
        } else break;
    }
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
