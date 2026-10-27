/* Exact cache implementation extracted from patched cram_io.c by the runner.
 * Static state is visible only in this test; production needs no test hooks. */
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cram_cache.inc"

#define CHECK(expression) do { \
    if (!(expression)) { \
        fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expression); \
        exit(1); \
    } \
} while (0)

static void invariant(void) {
    size_t total = 0;
    CHECK(cram_buffer_count >= 0 && cram_buffer_count <= 16);
    for (int i = 0; i < cram_buffer_count; i++) {
        unsigned peers = 0;
        CHECK(cram_buffers[i].data);
        CHECK(cram_buffers[i].size >= 65536 && cram_buffers[i].size <= 8388608);
        total += cram_buffers[i].size;
        for (int j = 0; j < cram_buffer_count; j++) {
            peers += cram_buffers[i].bucket == cram_buffers[j].bucket;
            CHECK(i == j || cram_buffers[i].data != cram_buffers[j].data);
        }
        CHECK(peers <= 2);
    }
    CHECK(total == cram_buffer_bytes && total <= 16777216);
}

static void clear(void) {
    /* Exercise the production drain, resetting only its stop flag for the next
     * isolated case. No workers exist in this state-inspection test. */
    cram_buffer_finish();
    CHECK(cram_buffer_count == 0 && cram_buffer_bytes == 0);
    cram_buffer_stopped = 0;
}

static void *allocate(size_t size) {
    void *data = cram_buffer_malloc(size);
    CHECK(data);
    memset(data, 0x5a, size); /* All requested bytes must be addressable. */
    invariant();
    return data;
}

int main(void) {
    /* GCC ASan's malloc_usable_size reports exact requested sizes, avoiding
     * allocator rounding at eligibility and excess-capacity boundaries. */
    const size_t mib = 1048576;
    void *large[3];
    for (int i = 0; i < 3; i++) large[i] = allocate(8 * mib);
    for (int i = 0; i < 3; i++) { cram_buffer_free(large[i]); invariant(); }
    CHECK(cram_buffer_bytes == 16 * mib && cram_buffer_count == 2);
    void *small = allocate(65536);
    cram_buffer_free(small);
    CHECK(cram_buffer_bytes == 16 * mib && cram_buffer_count == 2);
    clear();

    for (int i = 0; i < 3; i++) large[i] = allocate(mib);
    for (int i = 0; i < 3; i++) { cram_buffer_free(large[i]); invariant(); }
    CHECK(cram_buffer_count == 2);
    void *a = allocate(mib), *b = allocate(mib);
    CHECK(a != b && cram_buffer_count == 0); /* Exclusive live ownership. */
    free(a); free(b);

    const size_t bypass[] = {65535, 8 * mib + 1};
    for (size_t i = 0; i < sizeof(bypass) / sizeof(*bypass); i++) {
        void *data = allocate(bypass[i]);
        cram_buffer_free(data);
        CHECK(cram_buffer_count == 0 && cram_buffer_bytes == 0);
    }
    void *fit = allocate(mib + mib / 8);
    cram_buffer_free(fit);
    CHECK(cram_buffer_count == 1);
    a = allocate(mib);
    CHECK(a == fit && cram_buffer_count == 0); /* Inclusive 12.5% limit. */
    free(a);
    fit = allocate(mib + mib / 8 + 1);
    cram_buffer_free(fit);
    a = allocate(mib);
    CHECK(a != fit && cram_buffer_count == 1); /* Over-limit reuse rejected. */
    free(a);
    clear();

    a = allocate(mib);
    cram_buffer_free(a);
    cram_buffer_finish();
    CHECK(cram_buffer_stopped && cram_buffer_count == 0 && cram_buffer_bytes == 0);
    a = allocate(mib);
    cram_buffer_free(a);
    CHECK(cram_buffer_count == 0 && cram_buffer_bytes == 0);
    puts("CRAM cache: memory/peer caps, bypass, reuse boundary and shutdown passed");
    return 0;
}
