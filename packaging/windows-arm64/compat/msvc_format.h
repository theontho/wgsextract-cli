#ifndef WGSE_ARM64_MSVC_FORMAT_H
#define WGSE_ARM64_MSVC_FORMAT_H
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
static inline int wgse_vasprintf(char **buffer, const char *format, va_list arguments)
{
    if (!buffer) { errno = EINVAL; return -1; }
    *buffer = NULL;
    va_list copy;
    va_copy(copy, arguments);
    int length = _vscprintf(format, copy);
    va_end(copy);
    if (length < 0) return -1;
    char *result = (char *)malloc((size_t)length + 1);
    if (!result) { errno = ENOMEM; return -1; }
    va_copy(copy, arguments);
    int written = vsnprintf(result, (size_t)length + 1, format, copy);
    va_end(copy);
    if (written != length) {
        free(result);
        errno = EINVAL;
        return -1;
    }
    *buffer = result;
    return written;
}
static inline int wgse_asprintf(char **buffer, const char *format, ...)
{
    va_list arguments;
    va_start(arguments, format);
    int length = wgse_vasprintf(buffer, format, arguments);
    va_end(arguments);
    return length;
}
#define asprintf wgse_asprintf
#define vasprintf wgse_vasprintf
#endif
