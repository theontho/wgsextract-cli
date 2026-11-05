#pragma once

#include "safe_compat.h"
#include "sse2neon.h"

#define __rdtsc _rdtsc
#define _mm_prefetch(address, hint) __builtin_prefetch((address), 0, 3)
