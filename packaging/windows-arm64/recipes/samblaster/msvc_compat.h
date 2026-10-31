#ifndef WGSE_SAMBLASTER_ARM64_MSVC_COMPAT_H
#define WGSE_SAMBLASTER_ARM64_MSVC_COMPAT_H
#include "../bwa/msvc_compat.h"
#include "../../compat/msvc_format.h"
static inline void *wgse_mempcpy(void *dest, const void *src, size_t length)
{
    return (char *)memcpy(dest, src, length) + length;
}
#define mempcpy wgse_mempcpy
#endif
