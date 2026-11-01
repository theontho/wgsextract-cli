// OpenMP FPCR.FZ checks for the Arm64 PairHMM. Usage: omp_fpcr_state [legacy]
//
// 1. Cold pool: before any other OpenMP region, run the production arrangement of
//    computeLikelihoodsNative (WGSE_CALLER_FTZ_GUARD plus a guard inside the region),
//    then an unguarded region on the reused pool threads. Every thread, the caller
//    included, must have FZ set in the batch and its full original FPCR afterwards.
//    "legacy" instead sets FZ on the caller before the region (the pre-fix code) and
//    reports whether pool workers stay contaminated; exit 2 means they did.
// 2. Each worker runs with FZ set and restores its full prior FPCR, including a
//    non-default rounding mode.
#include <ScopedFtzDaz.h>
#include <omp.h>
#include <cstdio>
#include <cstring>

static const int kThreads = 4;

static int production_batch() {
    WGSE_CALLER_FTZ_GUARD;
    int bad = 0;
    #pragma omp parallel num_threads(kThreads) reduction(|:bad)
    {
        ScopedFtzDaz fpState;
        bad |= !(wgse_get_fpcr() & WGSE_FPCR_FZ);
    }
    return bad;
}

static int legacy_batch() {
    ScopedFtzDaz callerFpState;
    int bad = 0;
    #pragma omp parallel num_threads(kThreads) reduction(|:bad)
    {
        ScopedFtzDaz fpState;
        bad |= !(wgse_get_fpcr() & WGSE_FPCR_FZ);
    }
    return bad;
}

static int cold_pool_check(bool legacy) {
    const uint64_t original = wgse_get_fpcr();
    if (original & WGSE_FPCR_FZ) {
        std::fprintf(stderr, "FPCR.FZ already set at start: %#llx\n", (unsigned long long)original);
        return 1;
    }
    omp_set_dynamic(0);
    for (int batch = 0; batch < 2; ++batch) {
        if (legacy ? legacy_batch() : production_batch()) {
            std::fprintf(stderr, "FPCR.FZ not set inside batch %d\n", batch);
            return 1;
        }
    }
    uint64_t seen[kThreads] = {};
    int team = 0;
    #pragma omp parallel num_threads(kThreads)
    {
        seen[omp_get_thread_num()] = wgse_get_fpcr();
        #pragma omp single
        team = omp_get_num_threads();
    }
    int leaked = 0;
    for (int t = 0; t < team; ++t) {
        if (seen[t] != original) {
            std::fprintf(stderr, "%s: thread %d FPCR %#llx after batches, original %#llx\n",
                         legacy ? "legacy" : "production", t, (unsigned long long)seen[t],
                         (unsigned long long)original);
            ++leaked;
        }
    }
    if (team != kThreads) {
        std::fprintf(stderr, "expected a team of %d, got %d\n", kThreads, team);
        return 1;
    }
    if (legacy) {
        std::printf("legacy caller guard: %d of %d pool threads kept FZ\n", leaked, team);
        return leaked ? 2 : 0;
    }
    if (leaked) {
        return 1;
    }
    std::printf("FPCR cold-pool production arrangement passed: %d threads clean afterwards\n", team);
    return 0;
}

int main(int argc, char** argv) {
    const bool legacy = argc > 1 && std::strcmp(argv[1], "legacy") == 0;
    if (int status = cold_pool_check(legacy)) {
        return status;
    }
    if (legacy) {
        return 0;
    }
    const int counts[] = {1, 2, 4};
    for (int threads : counts) {
        int failed = 0;
        #pragma omp parallel num_threads(threads) reduction(|:failed)
        {
            const uint64_t original = wgse_get_fpcr();
            // Also set round-toward-zero (RMode=0b11) to prove all other bits are restored.
            const uint64_t baseline = (original & ~WGSE_FPCR_FZ) | (UINT64_C(3) << 22);
            for (int repeat = 0; repeat < 2; ++repeat) {
                wgse_set_fpcr(baseline);
                {
                    ScopedFtzDaz state;
                    if (wgse_get_fpcr() != (baseline | WGSE_FPCR_FZ)) {
                        failed = 1;
                    }
                }
                if (wgse_get_fpcr() != baseline) {
                    failed = 1;
                }
            }
            wgse_set_fpcr(original);
        }
        if (failed) {
            std::fprintf(stderr, "FPCR.FZ worker state/restoration failed: %d threads\n", threads);
            return 1;
        }
    }
    std::puts("FPCR.FZ worker state/restoration passed: 1,2,4 threads");
}
