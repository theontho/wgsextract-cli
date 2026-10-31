#ifndef WGSE_ARM64_MSVC_COMPAT_H
#define WGSE_ARM64_MSVC_COMPAT_H

#if !defined(_M_ARM64)
#error This recipe must compile native ARM64, not x64 or ARM64EC
#endif
#if defined(_M_ARM64EC) || defined(__SSE2__)
#error Emulated or hybrid x86 SIMD is not accepted
#endif
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#include <malloc.h>
typedef intptr_t ssize_t;
typedef int64_t wgse_off_t;
#define off_t wgse_off_t
#define alloca _alloca
#include "wgse_compat.h"

#endif
