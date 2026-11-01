#ifndef WGSE_INDEX_IO_H
#define WGSE_INDEX_IO_H

#include <errno.h>
#include <inttypes.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>

/* UCRT and libstdc++ must never receive a multi-gigabyte transfer at once. */
static const size_t wgse_index_io_chunk = (size_t)1 << 30;

static inline size_t wgse_index_read(void *buffer, size_t size, size_t count,
                                    FILE *stream, size_t chunk)
{
    if (!size || !count)
        return 0;
    if (!chunk || count > SIZE_MAX / size) {
        errno = !chunk ? EINVAL : EOVERFLOW;
        return 0;
    }
    if (chunk > wgse_index_io_chunk)
        chunk = wgse_index_io_chunk;
    const size_t total = size * count;
    size_t done = 0;
    while (done < total) {
        const size_t next = total - done < chunk ? total - done : chunk;
        const size_t read = fread((unsigned char *)buffer + done, 1, next, stream);
        done += read;
        if (read != next)
            break;
    }
    return done / size;
}

static inline size_t wgse_index_write(const void *buffer, size_t size, size_t count,
                                     FILE *stream, size_t chunk)
{
    if (!size || !count)
        return 0;
    if (!chunk || count > SIZE_MAX / size) {
        errno = !chunk ? EINVAL : EOVERFLOW;
        return 0;
    }
    if (chunk > wgse_index_io_chunk)
        chunk = wgse_index_io_chunk;
    const size_t total = size * count;
    size_t done = 0;
    while (done < total) {
        const size_t next = total - done < chunk ? total - done : chunk;
        const size_t written = fwrite((const unsigned char *)buffer + done,
                                      1, next, stream);
        done += written;
        if (written != next)
            break;
    }
    return done / size;
}

#ifdef __cplusplus
#include <ostream>
#include <stdexcept>

static inline void wgse_index_stream_write(
    std::ostream &stream, const char *buffer, size_t size,
    size_t chunk = wgse_index_io_chunk)
{
    if (!chunk)
        throw std::invalid_argument("Zero index I/O chunk size");
    if (chunk > wgse_index_io_chunk)
        chunk = wgse_index_io_chunk;
    size_t done = 0;
    while (done < size) {
        const size_t next = size - done < chunk ? size - done : chunk;
        stream.write(buffer + done, static_cast<std::streamsize>(next));
        if (!stream)
            throw std::ios_base::failure("Failed to write bwa-mem2 index");
        done += next;
    }
}

static inline void wgse_index_stream_flush(std::ostream &stream)
{
    stream.flush();
    if (!stream)
        throw std::ios_base::failure("Failed to flush bwa-mem2 index");
}
#endif

#endif
