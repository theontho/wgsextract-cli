#include "seeding_engine.h"

#include <algorithm>
#include <chrono>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <future>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include <zlib.h>

#include "kseq.h"
KSEQ_INIT(gzFile, gzread)

namespace {
using namespace wgse::seeding;

size_t positive(const char *text) {
    size_t used = 0;
    std::string value(text);
    unsigned long long result = std::stoull(value, &used);
    if (used != value.size() || value[0] == '-' || result == 0 || result > INT_MAX)
        throw std::runtime_error("expected a positive integer <= INT_MAX");
    return static_cast<size_t>(result);
}

Reads load_reads(const char *path, size_t limit) {
    gzFile file = gzopen(path, "rb");
    if (!file) throw std::runtime_error(std::string("cannot open FASTQ: ") + path);
    struct CloseFile {
        gzFile file;
        ~CloseFile() { gzclose(file); }
    } close{file};
    std::unique_ptr<kseq_t, decltype(&kseq_destroy)> record(kseq_init(file), kseq_destroy);
    Reads result;
    int status = 0;
    while (result.reads.size() < limit && (status = kseq_read(record.get())) >= 0) {
        if (record->seq.l > INT_MAX || result.bases.size() + record->seq.l > UINT32_MAX)
            throw std::runtime_error("read length or buffer exceeds integer limits");
        if (record->qual.l != record->seq.l)
            throw std::runtime_error("expected FASTQ with full quality strings");
        result.reads.push_back({static_cast<uint32_t>(result.bases.size()),
            static_cast<uint32_t>(record->seq.l)});
        for (size_t i = 0; i < record->seq.l; ++i) {
            unsigned char base = static_cast<unsigned char>(record->seq.s[i]);
            result.bases.push_back(nst_nt4_table[base]);
        }
    }
    int gzip_status = Z_OK;
    gzerror(file, &gzip_status);
    if (status < -1 || gzip_status != Z_OK || result.reads.empty())
        throw std::runtime_error("empty, truncated or invalid FASTQ");
    return result;
}

bool equal(const bwtintv_t &cpu, const SeedInterval &gpu) {
    return cpu.info == gpu.info && cpu.x[0] == gpu.x[0]
        && cpu.x[1] == gpu.x[1] && cpu.x[2] == gpu.x[2];
}

void verify(const std::vector<Intervals> &cpu, const std::vector<Intervals> &gpu) {
    for (size_t i = 0; i < cpu.size(); ++i) {
        if (cpu[i].size() != gpu[i].size())
            throw std::runtime_error("read " + std::to_string(i) + ": interval count "
                + std::to_string(cpu[i].size()) + " != " + std::to_string(gpu[i].size()));
        for (size_t j = 0; j < cpu[i].size(); ++j) {
            SeedInterval seed{};
            std::copy_n(gpu[i][j].x, 3, seed.x);
            seed.info = gpu[i][j].info;
            if (!equal(cpu[i][j], seed)) {
                fprintf(stderr, "read=%zu interval=%zu cpu=%llu,%llu,%llu,%llu "
                    "gpu=%llu,%llu,%llu,%llu\n", i, j,
                    cpu[i][j].x[0], cpu[i][j].x[1], cpu[i][j].x[2], cpu[i][j].info,
                    seed.x[0], seed.x[1], seed.x[2], seed.info);
                throw std::runtime_error("GPU/CPU interval mismatch");
            }
        }
    }
}

struct Partition {
    Reads input;
    std::vector<Intervals> reference, actual;

    void append(const Reads &reads, const std::vector<Intervals> &expected, size_t i) {
        const auto &read = reads.reads[i];
        input.reads.push_back({static_cast<uint32_t>(input.bases.size()), read.length});
        input.bases.insert(input.bases.end(), reads.bases.begin() + read.offset,
            reads.bases.begin() + read.offset + read.length);
        reference.push_back(expected[i]);
        actual.emplace_back();
    }
};

struct ConcurrentTime {
    double wall, cpu, gpu, fallback;
    size_t fallbacks;
};

struct CpuLocate {
    const bwt_t *bwt;
    const std::vector<uint64_t> &rows;
    std::vector<uint64_t> &positions;
    static constexpr long block = 4096;
    static void work(void *opaque, long i, int) {
        auto &job = *static_cast<CpuLocate *>(opaque);
        size_t end = std::min(job.rows.size(), size_t(i + 1) * block);
        for (size_t j = size_t(i) * block; j < end; ++j)
            job.positions[j] = bwt_sa(job.bwt, job.rows[j]);
    }
    void run(int threads) {
        positions.resize(rows.size());
        kt_for(threads, work, this, static_cast<long>((rows.size() + block - 1) / block));
    }
};

// Compares GPU suffix-array locate with upstream bwt_sa() for every row
// mem_chain() would visit, then times both.
void locate(const char *prefix, bwt_t &bwt, GpuSeeds &gpu, SeedKernel &kernel,
    const std::vector<Intervals> &seeds, const mem_opt_t &opt, size_t group, size_t repeat,
    size_t capacity) {
    std::string sa_path = std::string(prefix) + ".sa";
    bwt_restore_sa(sa_path.c_str(), &bwt);
    GpuLocate locator(kernel, capacity);
    if (!GpuLocate::supported(bwt)) throw std::runtime_error("unsupported suffix-array sampling");
    locator.sa = gpu.runtime.allocate_pages(bwt.n_sa * sizeof(uint64_t));
    if (!locator.sa) throw std::runtime_error("page-aligned SA allocation failed");
    memcpy(locator.sa.contents, bwt.sa, bwt.n_sa * sizeof(uint64_t));
    free(bwt.sa);
    bwt.sa = static_cast<uint64_t *>(locator.sa.contents);
    struct Detach {
        bwt_t &value;
        ~Detach() { value.sa = nullptr; }
    } detach{bwt};
    locator.configure(bwt);
    auto check = [&](const std::vector<uint64_t> &rows, std::vector<uint64_t> &expected,
        std::vector<uint64_t> &actual) {
        CpuLocate cpu{&bwt, rows, expected};
        cpu.run(9);
        locator.run(gpu.index, rows, actual, group);
        for (size_t i = 0; i < rows.size(); ++i) {
            if (expected[i] != actual[i]) {
                fprintf(stderr, "row=%llu cpu=%llu gpu=%llu\n", (unsigned long long)rows[i],
                    (unsigned long long)expected[i], (unsigned long long)actual[i]);
                throw std::runtime_error("GPU/CPU locate mismatch");
            }
        }
    };
    std::vector<uint64_t> rows, expected, actual;
    size_t counted = 0;
    for (const auto &intervals : seeds) {
        append_rows(intervals, opt.max_occ, rows);
        counted += row_count(intervals, opt.max_occ);
    }
    if (counted != rows.size()) throw std::runtime_error("row_count does not match append_rows");
    check(rows, expected, actual);
    printf("locate_parity=exact rows=%zu sa_intv=%d\n", rows.size(), bwt.sa_intv);
    if (bwt.seq_len < (uint64_t(1) << 24)) {
        // Small indexes: every row, including the primary and seq_len sentinels.
        std::vector<uint64_t> all(bwt.seq_len + 1), all_expected, all_actual;
        for (uint64_t k = 0; k <= bwt.seq_len; ++k) all[k] = k;
        check(all, all_expected, all_actual);
        printf("locate_all=exact rows=%zu primary=%llu\n", all.size(),
            static_cast<unsigned long long>(bwt.primary));
    }
    CpuLocate cpu{&bwt, rows, expected};
    for (size_t iteration = 0; iteration < repeat; ++iteration) {
        auto start = Clock::now();
        cpu.run(9);
        double cpu9 = seconds(start);
        start = Clock::now();
        locator.run(gpu.index, rows, actual, group);
        double wall = seconds(start);
        if (actual != expected) throw std::runtime_error("GPU/CPU locate mismatch");
        printf("locate_iteration=%zu rows=%zu cpu9_s=%.6f gpu_s=%.6f kernel_s=%.6f "
            "speedup9=%.3f\n", iteration + 1, rows.size(), cpu9, wall,
            locator.kernel_seconds, cpu9 / wall);
        fflush(stdout);
    }
}

ConcurrentTime concurrent(GpuSeeds &gpu, Partition &cpu_part, Partition &gpu_part,
    CpuJob &cpu, size_t group, int fallback_threads, const bwt_t *bwt, const mem_opt_t *opt) {
    auto start = Clock::now();
    // Disjoint reads and output vectors; only the immutable no-copy BWT is shared.
    // The future also joins the CPU worker if GPU dispatch throws.
    auto worker = std::async(std::launch::async, [&] {
        kt_for(9, CpuJob::work, &cpu, static_cast<long>(cpu_part.input.reads.size()));
        return seconds(start);
    });
    gpu.run(gpu_part.input, gpu_part.actual, group, fallback_threads, bwt, opt);
    double gpu_time = seconds(start);
    double cpu_time = worker.get();
    return {seconds(start), cpu_time, gpu_time, gpu.fallback_seconds, gpu.fallback};
}

int benchmark(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "Usage: seeding_benchmark <bwa-index-prefix> <fastq> "
            "[--reads N] [--batch N] [--repeat N] [--group N] [--verify-only] "
            "[--scratch-limit N] [--output-limit N] [--fallback-threads N] "
            "[--concurrent] [--gpu-percent N] [--locate] [--locate-batch N]\n");
        return 1;
    }
    size_t limit = 16384, batch = 8192, repeat = 3, group = 64;
    size_t scratch_limit = seed_scratch_capacity, output_limit = seed_output_capacity;
    size_t fallback_threads = 9;
    size_t gpu_percent = 50;
    size_t locate_batch = 1 << 22;
    bool verify_only = false, measure_concurrent = false, measure_locate = false;
    for (int i = 3; i < argc; ++i) {
        if (std::string(argv[i]) == "--verify-only") { verify_only = true; continue; }
        if (std::string(argv[i]) == "--concurrent") { measure_concurrent = true; continue; }
        if (std::string(argv[i]) == "--locate") { measure_locate = true; continue; }
        if (i + 1 == argc) throw std::runtime_error("missing option value");
        size_t value = positive(argv[i + 1]);
        std::string option = argv[i];
        if (option == "--reads") limit = value;
        else if (option == "--batch") batch = value;
        else if (option == "--repeat") repeat = value;
        else if (option == "--group") group = value;
        else if (option == "--scratch-limit") scratch_limit = value;
        else if (option == "--output-limit") output_limit = value;
        else if (option == "--fallback-threads") fallback_threads = value;
        else if (option == "--gpu-percent") gpu_percent = value;
        else if (option == "--locate-batch") locate_batch = value;
        else throw std::runtime_error("unknown option: " + option);
        ++i;
    }
    if (batch > 65536) throw std::runtime_error("--batch must be <= 65536");
    if (fallback_threads > 64) throw std::runtime_error("--fallback-threads must be <= 64");
    if (gpu_percent >= 100) throw std::runtime_error("--gpu-percent must be < 100");
    if (scratch_limit > seed_scratch_capacity || output_limit > seed_output_capacity)
        throw std::runtime_error("scratch/output limit exceeds compiled capacity");
    SeedKernel kernel;
    std::string reason;
    if (!kernel.available(reason)) {
        fprintf(stderr, "%s\n", reason.c_str());
        // Only hardware without a unified-memory Metal device may skip.
        return kernel.hardware_supported() ? 1 : 77;
    }
    GpuSeeds gpu(kernel, batch);
    printf("device=%s group=%zu batch=%zu max_threads=%lu scratch_limit=%zu "
        "output_limit=%zu fallback_threads=%zu\n",
        gpu.runtime.device().name.UTF8String, group, batch,
        static_cast<unsigned long>(gpu.pipeline.maxTotalThreadsPerThreadgroup),
        scratch_limit, output_limit, fallback_threads);
    std::unique_ptr<mem_opt_t, decltype(&free)> opt(mem_opt_init(), free);
    std::string bwt_path = std::string(argv[1]) + ".bwt";
    std::unique_ptr<bwt_t, decltype(&bwt_destroy)> bwt(bwt_restore_bwt(bwt_path.c_str()),
        bwt_destroy);
    gpu.load_index(*bwt, *opt, scratch_limit, output_limit);
    // The MTLBuffer owns the BWT now. Clear the C pointer before bwt_destroy,
    // including on exceptions. No SA is needed for interval collection.
    struct Detach {
        bwt_t *value;
        ~Detach() { value->bwt = nullptr; }
    } detach{bwt.get()};
    Reads input = load_reads(argv[2], limit);
    printf("reads=%zu bases=%zu bwt_bytes=%llu seq_len=%llu\n",
        input.reads.size(), input.bases.size(), bwt->bwt_size * 4, bwt->seq_len);
    std::vector<Intervals> reference(input.reads.size()), actual(input.reads.size());
    CpuJob one(bwt.get(), opt.get(), input, reference, 1);
    CpuJob nine(bwt.get(), opt.get(), input, actual, 9);
    // Untimed warm-up includes allocation, page faults and shader initialization.
    kt_for(1, CpuJob::work, &one, static_cast<long>(input.reads.size()));
    gpu.run(input, actual, group, int(fallback_threads), bwt.get(), opt.get());
    verify(reference, actual);
    Partition cpu_part, gpu_part;
    if (measure_concurrent) {
        for (size_t i = 0; i < input.reads.size(); ++i)
            (i % 100 < gpu_percent ? gpu_part : cpu_part).append(input, reference, i);
        if (cpu_part.input.reads.empty() || gpu_part.input.reads.empty())
            throw std::runtime_error("--concurrent needs reads in both partitions");
    }
    CpuJob concurrent_cpu(bwt.get(), opt.get(), cpu_part.input, cpu_part.actual, 9);
    if (measure_concurrent) {
        concurrent(gpu, cpu_part, gpu_part, concurrent_cpu, group,
            int(fallback_threads), bwt.get(), opt.get());
        verify(cpu_part.reference, cpu_part.actual);
        verify(gpu_part.reference, gpu_part.actual);
        printf("concurrent_parity=exact cpu_reads=%zu gpu_reads=%zu gpu_percent=%zu\n",
            cpu_part.input.reads.size(), gpu_part.input.reads.size(), gpu_percent);
        gpu.run(input, actual, group, int(fallback_threads), bwt.get(), opt.get());
    }
    if (measure_locate) {
        locate(argv[1], *bwt, gpu, kernel, reference, *opt, group, verify_only ? 0 : repeat,
            locate_batch);
        return 0;
    }
    if (verify_only) {
        printf("parity=exact intervals=%zu fallback=%zu length=%zu scratch=%zu output=%zu\n",
            gpu.intervals, gpu.fallback, gpu.reasons[seed_length_limit],
            gpu.reasons[seed_scratch_overflow], gpu.reasons[seed_output_overflow]);
        return 0;
    }
    for (size_t iteration = 0; iteration < repeat; ++iteration) {
        auto start = Clock::now();
        kt_for(1, CpuJob::work, &one, static_cast<long>(input.reads.size()));
        double cpu1 = seconds(start);
        start = Clock::now();
        kt_for(9, CpuJob::work, &nine, static_cast<long>(input.reads.size()));
        double cpu9 = seconds(start);
        verify(reference, actual);
        start = Clock::now();
        gpu.run(input, actual, group, int(fallback_threads), bwt.get(), opt.get());
        double wall = seconds(start);
        verify(reference, actual);
        printf("iteration=%zu parity=exact intervals=%zu fallback=%zu "
            "cpu1_s=%.6f cpu1_rps=%.1f cpu9_s=%.6f cpu9_rps=%.1f "
            "gpu_s=%.6f gpu_rps=%.1f kernel_s=%.6f fallback_s=%.6f "
            "pack_s=%.6f dispatch_s=%.6f unpack_s=%.6f "
            "length=%zu scratch=%zu output=%zu speedup9=%.3f\n",
            iteration + 1, gpu.intervals, gpu.fallback, cpu1, input.reads.size() / cpu1,
            cpu9, input.reads.size() / cpu9, wall, input.reads.size() / wall,
            gpu.kernel_seconds, gpu.fallback_seconds, gpu.pack_seconds,
            gpu.dispatch_seconds, gpu.unpack_seconds, gpu.reasons[seed_length_limit],
            gpu.reasons[seed_scratch_overflow], gpu.reasons[seed_output_overflow], cpu9 / wall);
        fflush(stdout);
        if (measure_concurrent) {
            auto timing = concurrent(gpu, cpu_part, gpu_part, concurrent_cpu, group,
                int(fallback_threads), bwt.get(), opt.get());
            verify(cpu_part.reference, cpu_part.actual);
            verify(gpu_part.reference, gpu_part.actual);
            printf("concurrent_iteration=%zu parity=exact combined_s=%.6f combined_rps=%.1f "
                "cpu_done_s=%.6f gpu_done_s=%.6f fallback_s=%.6f fallback=%zu "
                "gpu_reads=%zu cpu_reads=%zu speedup9=%.3f\n",
                iteration + 1, timing.wall, input.reads.size() / timing.wall,
                timing.cpu, timing.gpu, timing.fallback, timing.fallbacks,
                gpu_part.input.reads.size(), cpu_part.input.reads.size(), cpu9 / timing.wall);
            fflush(stdout);
        }
    }
    return 0;
}
} // namespace

int main(int argc, char **argv) {
    @autoreleasepool {
        try { return benchmark(argc, argv); }
        catch (const std::exception &error) {
            fprintf(stderr, "seeding_benchmark: %s\n", error.what());
            return 1;
        }
    }
}
