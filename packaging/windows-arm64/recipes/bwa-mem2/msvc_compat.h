#ifndef WGSE_MEM2_ARM64_MSVC_COMPAT_H
#define WGSE_MEM2_ARM64_MSVC_COMPAT_H
#if !defined(_M_ARM64) || defined(_M_ARM64EC)
#error BWA-MEM2 must target native ARM64
#endif
#if defined(__AVX__) || defined(__AVX2__) || defined(__AVX512BW__)
#error Only ARM64 NEON kernels are accepted
#endif
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <malloc.h>
typedef intptr_t ssize_t;
typedef int pid_t;
typedef int64_t wgse_off_t;
#define off_t wgse_off_t
#define alloca _alloca
#define S_ISREG(mode) (((mode) & _S_IFMT) == _S_IFREG)
#ifndef PATH_MAX
#define PATH_MAX 4096
#endif
#include "wgse_compat.h"
#include <windows.h>
#define sleep(seconds) Sleep((DWORD)(seconds) * 1000)
#ifdef __cplusplus
#include "sse2neon.h"
#define __rdtsc _rdtsc
#include "wgse_index_io.h"
#endif
#endif
