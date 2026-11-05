#include "bandedSWA.h"
#include "extension_batch.h"
#include "metal_backend.h"

#include <array>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <map>
#include <random>
#include <thread>
#include <vector>

uint64_t proc_freq = 24000000, tprof[LIM_R][LIM_C], prof[LIM_R];

namespace {
void append_pair(ExtensionBatch &out, const ExtensionBatch &source, int index) {
    SeqPair pair = source.pairs[index];
    out.reference.insert(out.reference.end(), source.reference.begin() + pair.idr,
        source.reference.begin() + pair.idr + pair.len1);
    out.query.insert(out.query.end(), source.query.begin() + pair.idq,
        source.query.begin() + pair.idq + pair.len2);
    pair.idr = static_cast<int32_t>(out.reference.size()) - pair.len1;
    pair.idq = static_cast<int32_t>(out.query.size()) - pair.len2;
    out.pairs.push_back(pair);
    out.header.parameters.count = static_cast<int32_t>(out.pairs.size());
}

int replay(int argc, char **argv) {
    int limit = 0, regroup = 0, repeats = 1, workers = 1;
    ExtensionKernel kernel = ExtensionKernel::row;
    for (int i = 3; i < argc; i += 2) {
        if (i + 1 == argc) throw std::runtime_error("Missing replay option value");
        std::string option = argv[i], value = argv[i + 1];
        if (option == "--limit") limit = std::stoi(value);
        else if (option == "--regroup") regroup = std::stoi(value);
        else if (option == "--repeats") repeats = std::stoi(value);
        else if (option == "--workers") workers = std::stoi(value);
        else if (option == "--kernel" && (value == "row" || value == "striped"
            || value == "shared" || value == "tiled"))
            kernel = value == "row" ? ExtensionKernel::row
                : value == "striped" ? ExtensionKernel::striped
                : value == "shared" ? ExtensionKernel::shared : ExtensionKernel::tiled;
        else throw std::runtime_error("Unknown replay option: " + option);
    }
    if (limit < 0 || regroup < 0 || regroup > 65536 || repeats < 1 || repeats > 100
        || workers < 1 || workers > 16)
        throw std::runtime_error("Replay option out of range");
    std::vector<std::filesystem::path> paths;
    for (const auto &entry : std::filesystem::directory_iterator(argv[2]))
        if (entry.path().extension() == ".bin") paths.push_back(entry.path());
    std::sort(paths.begin(), paths.end());
    if (paths.empty()) throw std::runtime_error("No captured batches");
    if (limit > 0 && paths.size() > size_t(limit)) paths.resize(limit);
    std::vector<ExtensionBatch> batches;
    std::map<std::vector<int>, ExtensionBatch> pending;
    for (const auto &path : paths) {
        ExtensionBatch batch = read_extension_batch(path.string());
        if (regroup == 0) {
            batches.push_back(std::move(batch));
            continue;
        }
        const auto &p = batch.header.parameters;
        std::vector<int> key{p.precision, p.bandwidth, p.open_del, p.extend_del,
            p.open_ins, p.extend_ins, p.zdrop, p.end_bonus};
        for (int value : p.matrix) key.push_back(value);
        auto &out = pending[key];
        for (int i = 0; i < p.count; ++i) {
            if (out.pairs.empty()) out.header = batch.header;
            append_pair(out, batch, i);
            if (out.header.parameters.count == regroup) {
                out.pairs.resize(size_t(regroup) + 64);
                batches.push_back(std::move(out));
                out = {};
            }
        }
    }
    for (auto &[key, batch] : pending) {
        (void)key;
        if (batch.pairs.empty()) continue;
        batch.pairs.resize(size_t(batch.header.parameters.count) + 64);
        batches.push_back(std::move(batch));
    }
    struct Totals {
        uint64_t batches = 0, pairs = 0, cells = 0, issued = 0;
        double cpu = 0, gpu = 0, execution = 0;
    };
    std::map<int, Totals> totals;
    wgse_set_kernel(kernel);
    for (auto &batch : batches) {
        const auto &p = batch.header.parameters;
        auto &total = totals[p.precision];
        BandedPairWiseSW oracle(p.open_del, p.extend_del, p.open_ins, p.extend_ins,
            p.zdrop, p.end_bonus, p.matrix, p.matrix[0], int8_t(-p.matrix[1]), 1);
        auto expected = batch.pairs;
        for (int backend = 0; backend < 2; ++backend) {
            wgse_set_backend(backend == 0 ? Backend::cpu : Backend::metal);
            auto actual = batch.pairs;
            auto start = std::chrono::steady_clock::now();
            uint64_t gpu_start = wgse_gpu_execution_ns(), cells_start = wgse_gpu_cells();
            uint64_t issued_start = wgse_gpu_issued_cells();
            for (int repeat = 0; repeat < repeats; ++repeat) {
                if (p.precision == 8)
                    oracle.getScores8(actual.data(), batch.reference.data(), batch.query.data(),
                        p.count, 1, p.bandwidth);
                else
                    oracle.getScores16(actual.data(), batch.reference.data(), batch.query.data(),
                        p.count, 1, p.bandwidth);
            }
            double elapsed = std::chrono::duration<double>(
                std::chrono::steady_clock::now() - start).count();
            if (backend == 0) {
                expected = actual;
                total.cpu += elapsed;
            } else {
                total.gpu += elapsed;
                total.execution += double(wgse_gpu_execution_ns() - gpu_start) / 1e9;
                total.cells += wgse_gpu_cells() - cells_start;
                total.issued += wgse_gpu_issued_cells() - issued_start;
                for (int i = 0; i < p.count; ++i) {
                    const auto &a = actual[i], &b = expected[i];
                    if (a.score != b.score || a.tle != b.tle || a.qle != b.qle
                        || a.gtle != b.gtle || a.gscore != b.gscore || a.max_off != b.max_off
                        || a.id != b.id || a.seqid != b.seqid || a.regid != b.regid) {
                        fprintf(stderr, "Replay mismatch bits=%d batch=%llu pair=%d len=%d,%d "
                            "h0=%d w=%d GPU=%d,%d,%d,%d,%d,%d CPU=%d,%d,%d,%d,%d,%d\n",
                            p.precision, static_cast<unsigned long long>(total.batches),
                            i, a.len1, a.len2, a.h0, p.bandwidth,
                            a.score, a.tle, a.qle, a.gtle, a.gscore, a.max_off,
                            b.score, b.tle, b.qle, b.gtle, b.gscore, b.max_off);
                        return 1;
                    }
                }
            }
        }
        total.pairs += uint64_t(p.count) * repeats;
        total.batches += repeats;
        batch.pairs = std::move(expected);
    }
    puts("bits\tbatches\tpairs\tcells\tcpu_us\tgpu_wall_us\tgpu_exec_us\tcpu_GCUPS\tgpu_GCUPS\tissued_cells");
    for (const auto &[bits, t] : totals)
        printf("%d\t%llu\t%llu\t%llu\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\t%llu\n", bits,
            static_cast<unsigned long long>(t.batches), static_cast<unsigned long long>(t.pairs),
            static_cast<unsigned long long>(t.cells), t.cpu * 1e6 / t.pairs,
            t.gpu * 1e6 / t.pairs, t.execution * 1e6 / t.pairs,
            t.cells / t.cpu / 1e9, t.cells / t.execution / 1e9,
            static_cast<unsigned long long>(t.issued));
    if (workers > 1) {
        std::atomic<size_t> next{0};
        std::atomic<bool> failed{false};
        wgse_set_backend(Backend::metal);
        uint64_t gpu_start = wgse_gpu_execution_ns(), cells_start = wgse_gpu_cells();
        auto start = std::chrono::steady_clock::now();
        std::vector<std::thread> threads;
        for (int worker = 0; worker < workers; ++worker) {
            threads.emplace_back([&] {
                for (;;) {
                    size_t index = next.fetch_add(1);
                    if (index >= batches.size() || failed.load()) break;
                    auto &batch = batches[index];
                    const auto &p = batch.header.parameters;
                    BandedPairWiseSW oracle(p.open_del, p.extend_del, p.open_ins, p.extend_ins,
                        p.zdrop, p.end_bonus, p.matrix, p.matrix[0], int8_t(-p.matrix[1]), 1);
                    auto actual = batch.pairs;
                    for (int repeat = 0; repeat < repeats; ++repeat) {
                        if (p.precision == 8)
                            oracle.getScores8(actual.data(), batch.reference.data(), batch.query.data(),
                                p.count, 1, p.bandwidth);
                        else
                            oracle.getScores16(actual.data(), batch.reference.data(), batch.query.data(),
                                p.count, 1, p.bandwidth);
                    }
                    for (int i = 0; i < p.count; ++i) {
                        const auto &a = actual[i], &b = batch.pairs[i];
                        if (a.score != b.score || a.tle != b.tle || a.qle != b.qle
                            || a.gtle != b.gtle || a.gscore != b.gscore || a.max_off != b.max_off)
                            failed = true;
                    }
                }
            });
        }
        for (auto &thread : threads) thread.join();
        if (failed) throw std::runtime_error("Concurrent replay mismatch");
        double wall = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
        double device = double(wgse_gpu_execution_ns() - gpu_start) / 1e9;
        uint64_t pairs = 0;
        for (const auto &[bits, t] : totals) { (void)bits; pairs += t.pairs; }
        printf("parallel\tworkers=%d\twall_s=%.4f\tgpu_sum_s=%.4f\twall_us=%.4f"
            "\tgpu_sum_us=%.4f\twall_GCUPS=%.4f\n", workers, wall, device,
            wall * 1e6 / pairs, device * 1e6 / pairs,
            (wgse_gpu_cells() - cells_start) / wall / 1e9);
    }
    wgse_print_backend_metrics();
    return 0;
}
}

int main(int argc, char **argv) {
    wgse_set_force_gpu(true);
    std::string reason;
    if (!wgse_metal_available(reason)) {
        fprintf(stderr, "%s\n", reason.c_str());
        // Skip only without suitable hardware; a broken backend must fail.
        return wgse_metal_hardware_supported() ? 1 : 77;
    }
    if (argc >= 3 && std::string(argv[1]) == "--replay") {
        try { return replay(argc, argv); }
        catch (const std::exception &error) {
            fprintf(stderr, "%s\n", error.what());
            return 1;
        }
    }
    std::array<int8_t, 25> matrix{};
    for (int i = 0; i < 5; ++i)
        for (int j = 0; j < 5; ++j)
            matrix[i * 5 + j] = int8_t(i == 4 || j == 4 ? -1 : i == j ? 1 : -4);
    BandedPairWiseSW oracle(6, 1, 6, 1, 100, 5, matrix.data(), 1, 4, 1);
    puts("bits\tpairs\tcpu_us_per_pair\tgpu_wall_us_per_pair\tgpu_exec_us_per_pair");
    for (int bits : {8, 16}) {
        for (int count : {256, 1024, 4096, 16384}) {
            std::mt19937 random(364);
            constexpr int stride = 384;
            std::vector<uint8_t> reference(count * stride), query(count * stride);
            // Upstream pads SIMD groups and reads ahead for prefetch.
            std::vector<SeqPair> source(count + 64);
            for (int i = 0; i < count; ++i) {
                auto &pair = source[i];
                pair.id = i;
                pair.idr = pair.idq = i * stride;
                pair.len2 = 30 + int(random() % (bits == 8 ? 61 : 121));
                pair.len1 = bits == 8
                    ? pair.len2 + int(random() % (127 - pair.len2))
                    : pair.len2 + int(random() % 201);
                pair.h0 = bits == 8 ? 20 + int(random() % 11)
                    : 20 + int(random() % 81);
                for (int j = 0; j < stride; ++j) {
                    reference[i * stride + j] = uint8_t(random() % 4);
                    query[i * stride + j] = random() % 100 < 97
                        ? reference[i * stride + j] : uint8_t(random() % 4);
                }
            }
            std::vector<SeqPair> expected;
            double elapsed[2]{}, execution = 0;
            int repeats[2]{};
            for (int backend = 0; backend < 2; ++backend) {
                auto pairs = source;
                wgse_set_backend(backend == 0 ? Backend::cpu : Backend::metal);
                for (int repeat = -2; repeat < 0 || elapsed[backend] < 0.25; ++repeat) {
                    auto start = std::chrono::steady_clock::now();
                    auto gpu_start = wgse_gpu_execution_ns();
                    if (bits == 8)
                        oracle.getScores8(pairs.data(), reference.data(), query.data(),
                            count, 1, 100);
                    else
                        oracle.getScores16(pairs.data(), reference.data(), query.data(),
                            count, 1, 100);
                    if (repeat >= 0) {
                        ++repeats[backend];
                        elapsed[backend] += std::chrono::duration<double>(
                            std::chrono::steady_clock::now() - start).count();
                        if (backend == 1)
                            execution += double(wgse_gpu_execution_ns() - gpu_start) / 1e9;
                    }
                }
                if (backend == 0) expected = pairs;
                else {
                    for (int i = 0; i < count; ++i) {
                        const auto &a = pairs[i], &b = expected[i];
                        if (a.score != b.score || a.tle != b.tle || a.qle != b.qle
                            || a.gtle != b.gtle || a.gscore != b.gscore
                            || a.max_off != b.max_off) {
                            fprintf(stderr, "SIMD mismatch bits=%d count=%d pair=%d\n",
                                bits, count, i);
                            return 1;
                        }
                    }
                }
            }
            if (execution == 0) {
                fputs("Benchmark unexpectedly fell back to CPU\n", stderr);
                return 1;
            }
            printf("%d\t%d\t%.4f\t%.4f\t%.4f\n", bits, count,
                elapsed[0] * 1e6 / (count * repeats[0]),
                elapsed[1] * 1e6 / (count * repeats[1]),
                execution * 1e6 / (count * repeats[1]));
            fflush(stdout);
        }
    }
    wgse_print_backend_metrics();
}
