// Floating-point environment and libm fingerprint for GKL PairHMM exactness.
// Prints identical keys on x86-64 and AArch64 so the two outputs can be diffed.
#include <cfloat>
#include <cinttypes>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "pairhmm/Context.h"
#include "wgse_libm.h"

#if defined(__aarch64__) || defined(_M_ARM64)
#define ARM 1
static uint64_t get_ctl() { uint64_t v; __asm__ __volatile__("mrs %0, fpcr" : "=r"(v)); return v; }
static void set_ctl(uint64_t v) { __asm__ __volatile__("msr fpcr, %0" : : "r"(v) : "memory"); }
static const uint64_t FTZ_ONLY = UINT64_C(1) << 24;  // FPCR.FZ: inputs and outputs
static const uint64_t FTZ_DAZ = UINT64_C(1) << 24;
static const char* ARCH = "aarch64";
#else
#include <immintrin.h>
static uint64_t get_ctl() { return _mm_getcsr(); }
static void set_ctl(uint64_t v) { _mm_setcsr(static_cast<unsigned>(v)); }
static const uint64_t FTZ_ONLY = 0x8000;  // MXCSR.FTZ (upstream GKL JNI thread)
static const uint64_t FTZ_DAZ = 0x8040;   // MXCSR.FTZ|DAZ (WGSE scoped OMP workers)
static const char* ARCH = "x86_64";
#endif

static uint32_t fbits(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }
static float ffrom(uint32_t u) { float f; std::memcpy(&f, &u, 4); return f; }
static uint64_t dbits(double d) { uint64_t u; std::memcpy(&u, &d, 8); return u; }

#if defined(__clang__)
#define OPAQUE __attribute__((noinline, optnone))
#else
#define OPAQUE __attribute__((noipa))
#endif

// Opaque so the compiler cannot move the arithmetic across control-register writes.
OPAQUE static float fmul(float a, float b) { return a * b; }
OPAQUE static float fadd(float a, float b) { return a + b; }
OPAQUE static double dmul(double a, double b) { return a * b; }

static uint64_t fnv(uint64_t h, uint64_t v) {
    for (int i = 0; i < 8; i++) { h ^= (v >> (8 * i)) & 0xff; h *= 1099511628211ULL; }
    return h;
}

static uint64_t bits(float f) { return fbits(f); }
static uint64_t bits(double d) { return dbits(d); }

template <class T, size_t N>
static uint64_t hash_table(const T (&table)[N]) {
    uint64_t h = 14695981039346656037ULL;
    for (size_t i = 0; i < N; i++) h = fnv(h, bits(table[i]));
    return h;
}

// Fingerprints the tables exactly as GKL initializes them (Context<T> constructor, including
// approximateLog10SumLog10 for matchToMatchProb), so equal hashes mean equal PairHMM tables.
template <class T>
static void context_tables(const char* label) {
    Context<T> ctx;
    typedef ContextBase<T> B;
    std::printf("context_%s_ph2pr\t%016" PRIx64 "\n", label, hash_table(B::ph2pr));
    std::printf("context_%s_jacobian_log\t%016" PRIx64 "\n", label, hash_table(B::jacobianLogTable));
    std::printf("context_%s_match_to_match\t%016" PRIx64 "\n", label, hash_table(B::matchToMatchProb));
    uint64_t h = 14695981039346656037ULL;
    h = fnv(fnv(fnv(h, bits(B::INITIAL_CONSTANT)), bits(B::LOG10_INITIAL_CONSTANT)), bits(B::RESULT_THRESHOLD));
    std::printf("context_%s_constants\t%016" PRIx64 "\n", label, h);
}

// Products whose exact value is below FLT_MIN but rounds (unbounded exponent) up to it.
// x86 detects tininess after rounding (keeps FLT_MIN); Arm with FPCR.AH=0 before rounding.
static void tininess(const char* label, uint64_t mode) {
    int total = 0, flushed = 0, minimum = 0;
    const uint64_t saved = get_ctl();
    for (uint32_t i = 0; i < 4096; i++) {
        for (uint32_t j = 0; j < 64; j++) {
            float fa = ffrom(0x3f7fffffu - i), fb = ffrom(0x00800000u + j);
            double exact = (double) fa * (double) fb;
            if (!(exact < FLT_MIN) || (float) (exact * 0x1p100) != (float) (0x1p-126 * 0x1p100)) {
                continue;
            }
            set_ctl(saved | mode);
            float r = fmul(fa, fb);
            set_ctl(saved);
            total++;
            flushed += r == 0.0f;
            minimum += fbits(r) == fbits(FLT_MIN);
        }
    }
    std::printf("tininess_%s\tcases=%d\tflushed_to_zero=%d\tFLT_MIN=%d\n", label, total, flushed, minimum);
}

static void denormals(const char* label, uint64_t mode) {
    const float sub = ffrom(0x00400000u);
    const uint64_t saved = get_ctl();
    set_ctl(saved | mode);
    float in_mul = fmul(sub, 1.0f);          // denormal input, exact denormal result
    float out_mul = fmul(0x1p-70f, 0x1p-70f); // normal inputs, denormal result
    float in_add = fadd(sub, 0x1p-120f);     // denormal operand into a normal sum
    double d_in = dmul(0x1p-1060, 1.0);
    set_ctl(saved);
    std::printf("denormal_%s\tin_mul=%08x\tout_mul=%08x\tin_add=%08x\tdouble_in=%016" PRIx64 "\n",
                label, fbits(in_mul), fbits(out_mul), fbits(in_add), dbits(d_in));
}

int main() {
    std::printf("arch\t%s\n", ARCH);
    std::printf("default_control\t0x%" PRIx64 "\n", get_ctl());
    denormals("off", 0);
    denormals("ftz_only", FTZ_ONLY);
    denormals("ftz_daz", FTZ_DAZ);
    tininess("off", 0);
    tininess("ftz_only", FTZ_ONLY);
    tininess("ftz_daz", FTZ_DAZ);

    context_tables<float>("float");
    context_tables<double>("double");

    // The libm calls Context.h makes (log10/log10f/pow via wgse_libm, as GKL calls them), to
    // localize a context_* mismatch; crt_* fingerprint the platform's own log10/log10f.
    uint64_t h_ph2pr = 14695981039346656037ULL, h_ph2prf = h_ph2pr, h_jac = h_ph2pr;
    uint64_t h_log10f = h_ph2pr, h_log10 = h_ph2pr, h_crtf = h_ph2pr, h_crt = h_ph2pr;
    for (int x = 0; x < 128; x++) {
        h_ph2pr = fnv(h_ph2pr, dbits(wgse_libm::pow(10.0, -((double) x) / 10.0)));
        h_ph2prf = fnv(h_ph2prf, fbits(powf(10.f, -((float) x) / 10.f)));
    }
    for (int k = 0; k <= 400000; k++) {  // JACOBIAN_LOG_TABLE_SIZE upper bound
        h_jac = fnv(h_jac, dbits(wgse_libm::log10(1.0 + wgse_libm::pow(10.0, -((double) k) * 0.0001))));
    }
    uint32_t u = 0x0d000000u;  // 1e-28 .. 1, every 97th float
    for (; u < 0x3f800000u; u += 97) {
        h_log10f = fnv(h_log10f, fbits(wgse_libm::log10f(ffrom(u))));
        h_crtf = fnv(h_crtf, fbits(log10f(ffrom(u))));
    }
    for (uint64_t v = 0x0010000000000000ULL; v < 0x3ff0000000000000ULL; v += 0x00000f0000000001ULL) {
        double d; std::memcpy(&d, &v, 8);
        h_log10 = fnv(h_log10, dbits(wgse_libm::log10(d)));
        h_crt = fnv(h_crt, dbits(std::log10(d)));
    }
    std::printf("libm_ph2pr_pow\t%016" PRIx64 "\n", h_ph2pr);
    std::printf("libm_ph2pr_powf\t%016" PRIx64 "\n", h_ph2prf);
    std::printf("libm_jacobian_log10_pow\t%016" PRIx64 "\n", h_jac);
    std::printf("libm_log10f_sweep\t%016" PRIx64 "\n", h_log10f);
    std::printf("libm_log10_sweep\t%016" PRIx64 "\n", h_log10);
    std::printf("crt_log10f_sweep\t%016" PRIx64 "\n", h_crtf);
    std::printf("crt_log10_sweep\t%016" PRIx64 "\n", h_crt);
    return 0;
}
