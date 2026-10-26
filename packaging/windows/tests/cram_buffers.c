/* Exercise real compressed blocks, concurrent reuse, and error recovery. */
#include "config.h"
#include "cram/cram.h"
#include <libdeflate.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(expression) do { \
    if (!(expression)) { \
        fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expression); \
        exit(1); \
    } \
} while (0)

static cram_block *block(const unsigned char *compressed, size_t compressed_size,
                         size_t raw_size) {
    cram_block *b = cram_new_block(EXTERNAL, 17);
    CHECK(b);
    b->data = malloc(compressed_size);
    CHECK(b->data);
    memcpy(b->data, compressed, compressed_size);
    b->method = b->orig_method = GZIP;
    b->comp_size = b->alloc = compressed_size;
    b->uncomp_size = raw_size;
    b->crc_part = 0;
    b->crc32 = libdeflate_crc32(0, b->data, compressed_size);
    b->crc32_checked = 0;
    return b;
}

static void *worker(void *argument) {
    size_t thread = (size_t)(uintptr_t)argument;
    struct libdeflate_compressor *compressor = libdeflate_alloc_compressor(1);
    CHECK(compressor);
    const size_t sizes[] = {65535, 65536, 1048576 + thread * 37, 3 * 1048576 + 17};
    for (int round = 0; round < 6; round++) {
        cram_block *held[4];
        for (size_t s = 0; s < 4; s++) {
            size_t length = sizes[s];
            unsigned char *raw = malloc(length);
            size_t bound = libdeflate_gzip_compress_bound(compressor, length);
            unsigned char *compressed = malloc(bound);
            CHECK(raw && compressed);
            uint32_t random = 13 + (uint32_t)thread + round;
            for (size_t i = 0; i < length; i++) {
                random = random * UINT32_C(1664525) + UINT32_C(1013904223);
                raw[i] = round & 1 ? random >> 24 : (i + thread + round) % 19;
            }
            size_t compressed_size = libdeflate_gzip_compress(
                compressor, raw, length, compressed, bound);
            CHECK(compressed_size > 8);
            held[s] = block(compressed, compressed_size, length);
            CHECK(cram_uncompress_block(held[s]) == 0);
            CHECK(held[s]->method == RAW && held[s]->alloc == length);
            CHECK(memcmp(held[s]->data, raw, length) == 0);
            /* Exercise the unknown-size inflate/realloc path too. */
            if (round == 0 && s == 2) {
                size_t actual_size = 0;
                char *out = zlib_mem_inflate((char *)compressed, compressed_size,
                                           &actual_size);
                CHECK(out && actual_size == length);
                CHECK(memcmp(out, raw, length) == 0);
                free(out); /* Pool pointers retain ordinary free compatibility. */
            }
            /* Error followed by success must not leave corrupt cached bytes. */
            if (round == 0 && s == 1) {
                compressed[compressed_size - 1] ^= 1;
                cram_block *bad = block(compressed, compressed_size, length);
                CHECK(cram_uncompress_block(bad) == -1);
                cram_free_block(bad);
                compressed[compressed_size - 1] ^= 1;
                cram_block *good = block(compressed, compressed_size, length);
                CHECK(cram_uncompress_block(good) == 0);
                CHECK(memcmp(good->data, raw, length) == 0);
                cram_free_block(good);
            }
            free(compressed);
            free(raw);
        }
        /* Keep several blocks live together before recycling them. */
        for (size_t s = 0; s < 4; s++)
            cram_free_block(held[s]);
    }
    libdeflate_free_compressor(compressor);
    return NULL;
}

int main(int argc, char **argv) {
#ifdef __SANITIZE_ADDRESS__
    if (argc == 2 && strcmp(argv[1], "--uaf-probe") == 0) {
        cram_block *b = cram_new_block(EXTERNAL, 17);
        CHECK(b);
        b->data = malloc(1048576);
        CHECK(b->data);
        volatile unsigned char *retired = b->data;
        cram_free_block(b);
        return retired[0]; /* Expected ASan use-after-poison in a separate run. */
    }
    if (argc == 2 && strcmp(argv[1], "--bounds-probe") == 0) {
        size_t length = 1048576;
        struct libdeflate_compressor *c = libdeflate_alloc_compressor(1);
        CHECK(c);
        void *raw = calloc(length, 1);
        size_t bound = libdeflate_gzip_compress_bound(c, length);
        void *compressed = malloc(bound);
        CHECK(raw && compressed);
        size_t compressed_size = libdeflate_gzip_compress(c, raw, length,
                                                        compressed, bound);
        CHECK(compressed_size);
        cram_block *b = cram_new_block(EXTERNAL, 17);
        CHECK(b);
        b->data = malloc(3 * length);
        CHECK(b->data);
        cram_free_block(b);
        char *out = zlib_mem_inflate(compressed, compressed_size, &length);
        CHECK(out && length == 1048576);
        volatile unsigned char *past_end = (unsigned char *)out + length;
        return past_end[0]; /* Cached capacity must not hide a logical overrun. */
    }
#else
    (void)argc; (void)argv;
#endif
    pthread_t threads[4];
    for (uintptr_t i = 0; i < 4; i++)
        CHECK(pthread_create(&threads[i], NULL, worker, (void *)i) == 0);
    for (size_t i = 0; i < 4; i++)
        CHECK(pthread_join(threads[i], NULL) == 0);
    puts("CRAM buffers: concurrent block roundtrips, boundaries, growth and error recovery passed");
    return 0;
}
