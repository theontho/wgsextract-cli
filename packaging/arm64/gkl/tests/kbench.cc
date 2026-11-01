// Kernel-only benchmark for the GKL PairHMM (AVX template) and Smith-Waterman (AVX2)
// kernels, built natively on x86-64 or with the Arm NEON port on AArch64.
// Usage: kbench SW_INPUT CASES REPS
// Prints timings and FNV-1a checksums of the exact result bits; x86-64 and AArch64
// builds must print identical hash_float, hash_double and hash_sw values.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>
#if defined(__x86_64__)
#include <xmmintrin.h>
#endif
#include "pairhmm/Context.h"
#include "pairhmm/avx_impl.h"
#include "pairhmm/pairhmm_common.h"
#include "smithwaterman/avx2_impl.h"
// GKL's own log10/log10f (the Arm patch routes them through wgse_libm).
#if __has_include("wgse_libm.h")
#include "wgse_libm.h"
static double gkl_log10(double x) { return wgse_libm::log10(x); }
static float gkl_log10f(float x) { return wgse_libm::log10f(x); }
#else
static double gkl_log10(double x) { return log10(x); }
static float gkl_log10f(float x) { return log10f(x); }
#endif
#undef min
#undef max

static uint64_t rng_state = 236;
static uint32_t next_u32() {
  rng_state = rng_state * 6364136223846793005ULL + 1442695040888963407ULL;
  return (uint32_t)(rng_state >> 33);
}
static int rnd(int n) { return (int)(next_u32() % (uint32_t)n); }
static const char BASES[] = "ACGT";

static std::string mutate(const std::string& s, double rate) {
  std::string out;
  for (char b : s) {
    double r = next_u32() / 4294967296.0;
    if (r < rate / 3) {
      out += BASES[rnd(4)];
    } else if (r < 2 * rate / 3) {
      out += b;
      for (int k = rnd(4); k >= 0; k--) out += BASES[rnd(4)];
    } else if (r >= rate) {
      out += b;
    }
  }
  if (out.empty()) out += BASES[rnd(4)];
  return out;
}

struct Case {
  std::string hap, rs, q, i, d, c;
};

static uint64_t fnv(uint64_t h, const void* p, size_t n) {
  const uint8_t* b = (const uint8_t*)p;
  for (size_t k = 0; k < n; k++) h = (h ^ b[k]) * 1099511628211ULL;
  return h;
}

static double now() {
  return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

int main(int argc, char** argv) {
  if (argc != 4) {
    fprintf(stderr, "usage: kbench SW_INPUT CASES REPS\n");
    return 2;
  }
#if defined(__aarch64__)
  uint64_t fpcr;
  __asm__ volatile("mrs %0, fpcr" : "=r"(fpcr));
  __asm__ volatile("msr fpcr, %0" ::"r"(fpcr | (1ULL << 24)));
#else
  _MM_SET_FLUSH_ZERO_MODE(_MM_FLUSH_ZERO_ON);
#endif
  ConvertChar::init();
  Context<float> ctxf;
  Context<double> ctxd;
  int ncases = atoi(argv[2]), reps = atoi(argv[3]);

  std::vector<Case> cases;
  while ((int)cases.size() < ncases) {
    int hl = 50 + rnd(550);
    std::string window;
    for (int k = 0; k < hl; k++) window += BASES[rnd(4)];
    std::vector<std::string> haps{window};
    for (int h = 1; h < 4; h++) haps.push_back(mutate(window, 0.005 + rnd(1000) / 1000.0 * 0.05));
    for (int r = 0; r < 25 && (int)cases.size() < ncases; r++) {
      const std::string& src = haps[rnd(4)];
      int len = std::min((int)src.size(), 1 + rnd(rnd(2) ? 151 : 300));
      int start = rnd((int)src.size() - len + 1);
      std::string read = mutate(src.substr(start, len), rnd(1000) / 1000.0 * 0.3);
      if (rnd(20) == 0) read[rnd((int)read.size())] = 'N';
      Case base;
      base.rs = read;
      bool lowq = rnd(10) == 0;
      for (size_t k = 0; k < read.size(); k++) {
        base.q += (char)(lowq ? 2 + rnd(10) : 6 + rnd(55));
        base.i += (char)(rnd(4) == 0 ? 10 + rnd(50) : 45);
        base.d += (char)(rnd(4) == 0 ? 10 + rnd(50) : 45);
        base.c += (char)10;
      }
      for (const std::string& h : haps) {
        Case c = base;
        c.hap = h;
        cases.push_back(c);
      }
    }
  }
  std::vector<testcase> tcs(cases.size());
  double cells = 0;
  for (size_t k = 0; k < cases.size(); k++) {
    Case& c = cases[k];
    testcase& t = tcs[k];
    t.rslen = (int)c.rs.size();
    t.haplen = (int)c.hap.size();
    t.q = c.q.c_str();
    t.i = c.i.c_str();
    t.d = c.d.c_str();
    t.c = c.c.c_str();
    t.hap = c.hap.c_str();
    t.rs = c.rs.c_str();
    cells += (double)t.rslen * t.haplen;
  }

  std::vector<double> results(tcs.size());
  double best_f = 1e30, best_d = 1e30;
  uint64_t hf = 1469598103934665603ULL, hd = hf;
  for (int rep = 0; rep < reps; rep++) {
    double t0 = now();
    for (size_t k = 0; k < tcs.size(); k++) {
      float f = compute_fp_avxs(&tcs[k]);
      results[k] = f < MIN_ACCEPTED ? gkl_log10(compute_fp_avxd(&tcs[k])) - ctxd.LOG10_INITIAL_CONSTANT
                                    : (double)(gkl_log10f(f) - ctxf.LOG10_INITIAL_CONSTANT);
    }
    best_f = std::min(best_f, now() - t0);
    if (rep == 0) hf = fnv(hf, results.data(), results.size() * sizeof(double));
    t0 = now();
    for (size_t k = 0; k < tcs.size(); k++) results[k] = compute_fp_avxd(&tcs[k]);
    best_d = std::min(best_d, now() - t0);
    if (rep == 0) hd = fnv(hd, results.data(), results.size() * sizeof(double));
  }

  std::vector<std::string> lines;
  std::ifstream in(argv[1]);
  for (std::string l; std::getline(in, l);) lines.push_back(l);
  double best_sw = 1e30;
  uint64_t hs = 1469598103934665603ULL;
  std::vector<char> cigar;
  for (int rep = 0; rep < reps; rep++) {
    double t0 = now();
    for (int strategy = SOFTCLIP; strategy <= IGNORE; strategy++) {
      for (size_t k = 0; k + 1 < lines.size(); k += 2) {
        std::string& a = lines[k];
        std::string& b = lines[k + 1];
        cigar.assign(2 * std::max(a.size(), b.size()), 0);
        uint32_t count = 0;
        int32_t offset = 0;
        int32_t r = runSWOnePairBT_fp_avx2(200, -150, -260, -11, (uint8_t*)a.data(), (uint8_t*)b.data(),
                                           (int16_t)a.size(), (int16_t)b.size(), (int8_t)strategy, cigar.data(),
                                           (int32_t)cigar.size(), &count, &offset);
        if (rep == 0) {
          hs = fnv(hs, &r, sizeof r);
          hs = fnv(hs, &offset, sizeof offset);
          hs = fnv(hs, cigar.data(), strnlen(cigar.data(), cigar.size()));
        }
      }
    }
    best_sw = std::min(best_sw, now() - t0);
  }
  printf("{\"cases\": %zu, \"cells\": %.0f, \"float_s\": %.4f, \"float_gcups\": %.3f, \"double_s\": %.4f, "
         "\"double_gcups\": %.3f, \"sw_pairs_x4\": %zu, \"sw_s\": %.4f, \"hash_float\": \"%016llx\", "
         "\"hash_double\": \"%016llx\", \"hash_sw\": \"%016llx\"}\n",
         tcs.size(), cells, best_f, cells / best_f / 1e9, best_d, cells / best_d / 1e9, lines.size() / 2, best_sw,
         (unsigned long long)hf, (unsigned long long)hd, (unsigned long long)hs);
  return 0;
}
