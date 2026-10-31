#ifndef WGSE_MINIMAP2_ARM64_MSVC_H
#define WGSE_MINIMAP2_ARM64_MSVC_H

#if !defined(_M_ARM64) || defined(_M_ARM64EC)
#error Native ARM64 code generation is required
#endif
#include <stdint.h>
#include <stddef.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <malloc.h>
typedef intptr_t ssize_t;
typedef int64_t wgse_off_t;
#define off_t wgse_off_t
#define alloca _alloca
#define fseeko _fseeki64
#define ftello _ftelli64
#include "wgse_compat.h"

#endif
