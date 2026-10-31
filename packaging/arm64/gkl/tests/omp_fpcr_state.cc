// Each OpenMP worker must run the kernel with FPCR.FZ set and restore its full prior FPCR.
#include <ScopedFtzDaz.h>
#include <omp.h>
#include <cstdio>

int main() {
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
