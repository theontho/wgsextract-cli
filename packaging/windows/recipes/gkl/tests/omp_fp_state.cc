#include <ScopedFtzDaz.h>
#include <omp.h>
#include <cstdio>

int main() {
    const int counts[] = {1, 2, 4};
    for (int threads : counts) {
        int failed = 0;
        #pragma omp parallel num_threads(threads) reduction(|:failed)
        {
            const unsigned original = _mm_getcsr();
            const unsigned baseline = (original & ~0x8040u) | 0x2000u;
            for (int repeat = 0; repeat < 2; ++repeat) {
                _mm_setcsr(baseline);
                {
                    ScopedFtzDaz state;
                    if (_mm_getcsr() != (baseline | 0x8040u)) {
                        failed = 1;
                    }
                }
                if (_mm_getcsr() != baseline) {
                    failed = 1;
                }
            }
            _mm_setcsr(original);
        }
        if (failed) {
            std::fprintf(stderr, "FTZ/DAZ worker state/restoration failed: %d threads\n", threads);
            return 1;
        }
    }
    std::puts("FTZ/DAZ worker state/restoration passed: 1,2,4 threads");
}
