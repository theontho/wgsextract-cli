#include "bandedSWA.h"
#include "metal_backend.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

uint64_t proc_freq = 24000000, tprof[LIM_R][LIM_C], prof[LIM_R];

bool concurrent_parity() {
    constexpr int workers = 8, count = 512, stride = 128;
    struct Input {
        std::vector<SeqPair> pairs, expected;
        std::vector<uint8_t> reference, query;
        std::array<int8_t, 25> matrix{};
        int bandwidth, open, zdrop, bonus, precision;
    };
    std::array<Input, workers> inputs;
    std::mt19937 random(365);
    wgse_set_aggregation(4096, 10000, 8192);
    for (int round = 0; round < 5; ++round) {
        wgse_set_force_gpu(true);
        wgse_set_backend(Backend::cpu);
        for (int worker = 0; worker < workers; ++worker) {
            auto &input = inputs[worker];
            input.bandwidth = std::array<int, 4>{7, 31, 100, 200}[worker % 4];
            input.open = round == 1 ? 3 + worker : 6;
            input.zdrop = round == 1 ? 10 + worker : 100;
            input.bonus = round == 1 ? worker : 5;
            input.precision = round == 1 ? std::array<int, 3>{0, 8, 16}[worker % 3]
                : round == 4 && worker % 2 == 0 ? 8 : 16;
            for (int i = 0; i < 5; ++i)
                for (int j = 0; j < 5; ++j)
                    input.matrix[i * 5 + j] = int8_t(i == 4 || j == 4 ? -1
                        : i == j ? 1 : round == 1 ? -2 - worker : -4);
            if (input.precision == 0) input.matrix[1] = -9;
            input.reference.resize(count * stride);
            input.query.resize(count * stride);
            input.pairs.assign(count + 64, {});
            for (int i = 0; i < count; ++i) {
                auto &pair = input.pairs[i];
                pair.idr = pair.idq = i * stride;
                pair.len1 = 80 + int(random() % 40);
                pair.len2 = 65 + int(random() % 32);
                pair.h0 = 5;
                pair.id = i;
                pair.seqid = i + 19;
                pair.regid = i + 23;
                if (round == 2 && i % 31 == 0) pair.len2 = 0;
                for (int j = 0; j < stride; ++j) {
                    input.reference[pair.idr + j] = uint8_t(random() % 5);
                    input.query[pair.idq + j] = i % 2 == 0
                        ? input.reference[pair.idr + j] : uint8_t(random() % 5);
                }
            }
            input.expected = input.pairs;
            BandedPairWiseSW oracle(input.open, 1, input.open, 1, input.zdrop,
                input.bonus, input.matrix.data(), 1, int8_t(round == 1 ? 2 + worker : 4), 1);
            if (input.precision == 8)
                oracle.getScores8(input.expected.data(), input.reference.data(),
                    input.query.data(), count, 1, input.bandwidth);
            else if (input.precision == 16)
                oracle.getScores16(input.expected.data(), input.reference.data(),
                    input.query.data(), count, 1, input.bandwidth);
            else
                oracle.scalarBandedSWAWrapper(input.expected.data(), input.reference.data(),
                    input.query.data(), count, 1, input.bandwidth);
        }
        wgse_set_backend(Backend::metal);
        wgse_set_routing(128, 8);
        if (round == 3) {
            wgse_set_force_gpu(false);
            wgse_set_routing(128, 1);
        }
        uint64_t before = wgse_gpu_dispatches();
        std::mutex mutex;
        std::condition_variable changed;
        int arrived = 0;
        std::atomic<bool> success{true};
        std::atomic<int> accepted{0};
        std::vector<std::thread> threads;
        for (auto &input : inputs) {
            threads.emplace_back([&] {
                {
                    std::unique_lock<std::mutex> lock(mutex);
                    ++arrived;
                    changed.notify_all();
                    changed.wait(lock, [&] { return arrived == workers; });
                }
                bool gpu = wgse_metal_extend(input.pairs.data(), input.reference.data(),
                    input.query.data(), count, input.bandwidth, input.open, 1,
                    input.open, 1, input.zdrop, input.bonus, input.matrix.data(),
                    input.precision);
                if (gpu) ++accepted;
                else if (round != 3) success = false;
                for (int i = 0; i < count; ++i) {
                    auto &a = input.pairs[i], &b = input.expected[i];
                    if (gpu ? (a.score != b.score || a.tle != b.tle || a.qle != b.qle
                        || a.gtle != b.gtle || a.gscore != b.gscore || a.max_off != b.max_off)
                        : (a.score || a.tle || a.qle || a.gtle || a.gscore || a.max_off))
                        success = false;
                    if (a.id != i || a.idr != i * stride || a.idq != i * stride
                        || a.seqid != i + 19 || a.regid != i + 23) success = false;
                }
            });
        }
        for (auto &thread : threads) thread.join();
        if (!success || (round != 1 && wgse_gpu_dispatches() - before >= workers)
            || (round == 3 && (accepted == 0 || accepted == workers))) {
            fprintf(stderr, "Concurrent parity/aggregation failed round=%d dispatches=%llu\n",
                round, static_cast<unsigned long long>(wgse_gpu_dispatches() - before));
            return false;
        }
    }
    wgse_set_force_gpu(false);
    wgse_set_routing(128, 16);
    wgse_set_aggregation(8192, 100, 32768);
    auto &input = inputs[0];
    auto before = input.pairs;
    wgse_set_gpu8(false, 8192);
    if (wgse_metal_extend(input.pairs.data(), input.reference.data(),
        input.query.data(), count, 100, 6, 1, 6, 1, 100, 5, input.matrix.data(), 8)
        || memcmp(input.pairs.data(), before.data(), before.size() * sizeof(SeqPair)) != 0) {
        fputs("Hybrid 8-bit routing modified the CPU input\n", stderr);
        return false;
    }
    wgse_set_gpu8(true, 8192);
    if (wgse_metal_extend(input.pairs.data(), input.reference.data(),
        input.query.data(), count, input.bandwidth, input.open, 1, input.open, 1,
        input.zdrop, input.bonus, input.matrix.data(), 8)
        || memcmp(input.pairs.data(), before.data(), before.size() * sizeof(SeqPair)) != 0) {
        fputs("Unpaired 8-bit latency fallback modified the CPU input\n", stderr);
        return false;
    }
    wgse_set_force_gpu(true);
    uint64_t dispatches = wgse_gpu_dispatches();
    if (!wgse_metal_extend(input.pairs.data(), input.reference.data(),
        input.query.data(), count, input.bandwidth, input.open, 1, input.open, 1,
        input.zdrop, input.bonus, input.matrix.data(), 8)
        || wgse_gpu_dispatches() == dispatches) {
        fputs("Hybrid 8-bit aggregation did not reach the GPU\n", stderr);
        return false;
    }
    wgse_set_force_gpu(false);
    bool helped = false, help_ok = false;
    auto source = input.pairs;
    WgseExtensionWork pending([&](bool independent) {
        helped = independent;
        auto scratch = wgse_extension_scratch(source.data(), count, 512);
        WgseExtensionScope nested(count);
        help_ok = wgse_metal_extend(scratch.pairs, input.reference.data(),
            input.query.data(), count, input.bandwidth, input.open, 1, input.open, 1,
            input.zdrop, input.bonus, input.matrix.data(), 8);
        for (int i = 0; i < count; ++i) {
            const auto &a = scratch.pairs[i], &b = input.expected[i];
            help_ok &= a.score == b.score && a.qle == b.qle && a.tle == b.tle
                && a.gscore == b.gscore && a.gtle == b.gtle && a.max_off == b.max_off;
        }
    });
    bool outer_ok;
    wgse_set_aggregation(1, 0, 32768);
    uint64_t before_bundle = wgse_gpu_dispatches();
    {
        WgseExtensionScope outer(count);
        outer_ok = wgse_metal_extend(input.pairs.data(), input.reference.data(),
            input.query.data(), count, input.bandwidth, input.open, 1, input.open, 1,
            input.zdrop, input.bonus, input.matrix.data(), 16);
    }
    pending.finish();
    if (!helped || !help_ok || !outer_ok || wgse_gpu_dispatches() != before_bundle + 1) {
        fputs("Independent extension work did not overlap the pending request\n", stderr);
        return false;
    }
    wgse_set_aggregation(8192, 100, 32768);
    return true;
}

int main(int argc, char **argv) {
    wgse_set_force_gpu(true);
    if (argc == 2 && std::string(argv[1]) == "tiled")
        wgse_set_kernel(ExtensionKernel::tiled);
    if (argc == 2 && std::string(argv[1]) == "striped")
        wgse_set_kernel(ExtensionKernel::striped);
    std::string reason;
    if (!wgse_metal_available(reason)) {
        fprintf(stderr, "Metal required for parity test: %s\n", reason.c_str());
        return 77;
    }
    wgse_set_backend(Backend::metal);
    std::mt19937 random(364);
    constexpr int batch_size = 512;
    constexpr int stride = 512;
    std::vector<uint8_t> reference(batch_size * stride), query(batch_size * stride);
    std::vector<SeqPair> pairs(batch_size);
    for (int iteration = 0; iteration < 32; ++iteration) {
        std::array<int8_t, 25> matrix{};
        int match = 1 + int(random() % 3), mismatch = 1 + int(random() % 5);
        if (iteration == 31) match = 127;
        for (int i = 0; i < 5; ++i)
            for (int j = 0; j < 5; ++j)
                matrix[i * 5 + j] = int8_t(i == 4 || j == 4 ? -1
                    : i == j ? match : -mismatch);
        if (iteration == 1) matrix[1] = -7;
        int open_del = int(random() % 8), open_ins = int(random() % 8);
        int extend_del = 1 + int(random() % 3), extend_ins = 1 + int(random() % 3);
        int zdrop = iteration % 3 == 0 ? 0 : 1 + int(random() % 40);
        int end_bonus = int(random() % 12), bandwidth = int(random() % 100);
        BandedPairWiseSW oracle(open_del, extend_del, open_ins, extend_ins,
            zdrop, end_bonus, matrix.data(), int8_t(match), int8_t(mismatch), 1);
        for (int i = 0; i < batch_size; ++i) {
            pairs[i] = {};
            auto &pair = pairs[i];
            pair.idr = pair.idq = i * stride;
            pair.id = i;
            pair.seqid = i + 7;
            pair.regid = i + 11;
            pair.len1 = i % 17 == 0 ? 0 : 1 + int(random() % 384);
            pair.len2 = i % 4 == 0 ? 256 : 1 + int(random() % 256);
            pair.h0 = 1 + int(random() % 300);
            if (i % 29 == 0) pair.h0 = 32767;
            if (i % 31 == 0) pair.h0 = 1;
            for (int j = 0; j < stride; ++j) {
                reference[i * stride + j] = uint8_t(random() % 5);
                query[i * stride + j] = iteration % 2 == 0
                    ? reference[i * stride + j] : uint8_t(random() % 5);
                if (iteration == 31)
                    reference[i * stride + j] = query[i * stride + j] = 0;
            }
        }
        auto expected = pairs;
        oracle.scalarBandedSWAWrapper(expected.data(), reference.data(),
            query.data(), batch_size, 1, bandwidth);
        if (!wgse_metal_extend(pairs.data(), reference.data(), query.data(),
            batch_size, bandwidth, open_del, extend_del, open_ins, extend_ins,
            zdrop, end_bonus, matrix.data())) {
            fprintf(stderr, "Parity test unexpectedly fell back to CPU\n");
            return 1;
        }
        for (int i = 0; i < batch_size; ++i) {
            const auto &a = pairs[i], &b = expected[i];
            if (a.score != b.score || a.tle != b.tle || a.qle != b.qle
                || a.gtle != b.gtle || a.gscore != b.gscore || a.max_off != b.max_off
                || a.id != b.id || a.idr != b.idr || a.idq != b.idq
                || a.seqid != b.seqid || a.regid != b.regid) {
                fprintf(stderr, "Parity mismatch iteration=%d job=%d: "
                    "GPU %d,%d,%d,%d,%d,%d CPU %d,%d,%d,%d,%d,%d\n",
                    iteration, i, a.score, a.tle, a.qle, a.gtle, a.gscore,
                    a.max_off, b.score, b.tle, b.qle, b.gtle, b.gscore, b.max_off);
                fprintf(stderr, "len=%d,%d h0=%d w=%d gaps=%d,%d,%d,%d z=%d bonus=%d\n",
                    a.len1, a.len2, a.h0, bandwidth, open_del, extend_del,
                    open_ins, extend_ins, zdrop, end_bonus);
                return 1;
            }
        }
    }
    for (int bits : {8, 16}) {
        for (int bandwidth : {0, 1, 7, 31, 100, 200, 512}) {
            std::array<int8_t, 25> matrix{};
            for (int i = 0; i < 5; ++i)
                for (int j = 0; j < 5; ++j)
                    matrix[i * 5 + j] = int8_t(i == 4 || j == 4 ? -1
                        : i == j ? 1 : -4);
            BandedPairWiseSW oracle(6, 1, 6, 1, 100, 5, matrix.data(), 1, 4, 1);
            std::vector<SeqPair> actual(batch_size + 64);
            for (int i = 0; i < batch_size; ++i) {
                auto &pair = actual[i];
                pair.id = i;
                pair.idr = pair.idq = i * stride;
                pair.len1 = i % 17 == 0 ? 0 : 1 + int(random() % (bits == 8 ? 126 : 384));
                pair.len2 = i % 19 == 0 ? 0 : 1 + int(random() % (bits == 8 ? 126 : 256));
                int limit = (bits == 8 ? 127 : 32767) - std::min(pair.len1, pair.len2);
                pair.h0 = bandwidth == 512 && i % 5 == 0 ? limit
                    : 1 + int(random() % std::min({limit, 100, bandwidth + 7}));
                for (int j = 0; j < stride; ++j) {
                    reference[i * stride + j] = uint8_t(random() % 5);
                    query[i * stride + j] = i % 2 == 0
                        ? reference[i * stride + j] : uint8_t(random() % 5);
                }
            }
            auto expected = actual;
            for (int backend = 0; backend < 2; ++backend) {
                wgse_set_backend(backend == 0 ? Backend::cpu : Backend::metal);
                auto &jobs = backend == 0 ? expected : actual;
                uint64_t before = wgse_gpu_execution_ns();
                if (bits == 8)
                    oracle.getScores8(jobs.data(), reference.data(), query.data(),
                        batch_size, 1, bandwidth);
                else
                    oracle.getScores16(jobs.data(), reference.data(), query.data(),
                        batch_size, 1, bandwidth);
                if (backend == 1 && wgse_gpu_execution_ns() == before) {
                    fprintf(stderr, "SIMD parity unexpectedly used CPU bits=%d band=%d\n",
                        bits, bandwidth);
                    return 1;
                }
            }
            for (int i = 0; i < batch_size; ++i) {
                const auto &a = actual[i], &b = expected[i];
                if (a.score != b.score || a.tle != b.tle || a.qle != b.qle
                    || a.gtle != b.gtle || a.gscore != b.gscore || a.max_off != b.max_off) {
                    fprintf(stderr, "SIMD mismatch bits=%d band=%d job=%d len=%d,%d h0=%d: "
                        "GPU %d,%d,%d,%d,%d,%d CPU %d,%d,%d,%d,%d,%d\n",
                        bits, bandwidth, i, a.len1, a.len2, a.h0,
                        a.score, a.tle, a.qle, a.gtle, a.gscore, a.max_off,
                        b.score, b.tle, b.qle, b.gtle, b.gscore, b.max_off);
                    return 1;
                }
            }
        }
    }
    std::array<int8_t, 25> matrix{};
    for (int i = 0; i < 5; ++i)
        for (int j = 0; j < 5; ++j)
            matrix[i * 5 + j] = int8_t(i == 4 || j == 4 ? -1 : i == j ? 1 : -4);
    SeqPair empty{};
    empty.h0 = 100;
    if (!wgse_metal_extend(&empty, reference.data(), query.data(), 1, 100,
        6, 1, 6, 1, 100, 5, matrix.data(), 16)
        || empty.score != 100 || empty.tle || empty.qle || empty.gtle
        || empty.gscore != -1 || empty.max_off) {
        fputs("Empty extension result mismatch\n", stderr);
        return 1;
    }
    struct FallbackCase {
        int h0, bandwidth, extend, zdrop, bits;
    };
    for (const auto &test : {FallbackCase{20, 100, 1, 0, 8},
        FallbackCase{20, 100, 2, 100, 16}, FallbackCase{49, 7, 1, 100, 16},
        FallbackCase{120, 100, 1, 100, 8}, FallbackCase{32768, 100, 1, 100, 16}}) {
        SeqPair pair{};
        pair.len1 = pair.len2 = 41;
        pair.h0 = test.h0;
        pair.score = -999;
        if (wgse_metal_extend(&pair, reference.data(), query.data(), 1,
            test.bandwidth, 6, test.extend, 6, test.extend, test.zdrop, 5,
            matrix.data(), test.bits) || pair.score != -999) {
            fputs("Unsupported SIMD input was accepted or modified\n", stderr);
            return 1;
        }
    }
    if (!concurrent_parity()) return 1;
    wgse_print_backend_metrics();
    puts("Scalar, SIMD, mixed-parameter concurrent aggregation and hybrid routing parity pass");
}
