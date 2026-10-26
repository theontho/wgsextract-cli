/* Check small-sort allocation removal against independent qsort oracles. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static size_t allocations;
static void *counted_malloc(size_t size) {
    allocations++;
    return malloc(size);
}

#define malloc counted_malloc
#include "htslib/ksort.h"
#undef malloc

#define CHECK(expression) do { \
    if (!(expression)) { \
        fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expression); \
        exit(1); \
    } \
} while (0)

typedef struct { int32_t key; uint32_t ordinal; } row_t;
#define row_less(a, b) ((a).key < (b).key || \
    ((a).key == (b).key && (a).ordinal < (b).ordinal))
KSORT_INIT_STATIC_GENERIC(uint16_t)
KSORT_INIT_STATIC_GENERIC(int64_t)
KSORT_INIT_STATIC(row, row_t, row_less)

static int compare_u16(const void *left, const void *right) {
    uint16_t a = *(const uint16_t *)left, b = *(const uint16_t *)right;
    return (a > b) - (a < b);
}
static int compare_i64(const void *left, const void *right) {
    int64_t a = *(const int64_t *)left, b = *(const int64_t *)right;
    return (a > b) - (a < b);
}
static int compare_row(const void *left, const void *right) {
    row_t a = *(const row_t *)left, b = *(const row_t *)right;
    return row_less(b, a) - row_less(a, b);
}

static void check(size_t n, int pattern) {
    size_t capacity = n ? n : 1;
    uint16_t *u16 = malloc(capacity * sizeof(*u16));
    uint16_t *want_u16 = malloc(capacity * sizeof(*want_u16));
    int64_t *i64 = malloc(capacity * sizeof(*i64));
    int64_t *want_i64 = malloc(capacity * sizeof(*want_i64));
    row_t *row = malloc(capacity * sizeof(*row));
    row_t *want_row = malloc(capacity * sizeof(*want_row));
    CHECK(u16 && want_u16 && i64 && want_i64 && row && want_row);
    uint32_t random = 31;
    for (size_t i = 0; i < n; i++) {
        random = random * UINT32_C(1664525) + UINT32_C(1013904223);
        uint32_t value = pattern == 0 ? random : pattern == 1 ? i :
            pattern == 2 ? n - i : pattern == 3 ? 0 :
            pattern == 4 ? i % 2 : pattern == 5 ? random % 17 :
            i < n / 2 ? i : n - i;
        u16[i] = value;
        i64[i] = (int64_t)((uint64_t)value << 32 | (uint32_t)~value);
        row[i].key = (int32_t)(value % 17) - 8;
        row[i].ordinal = i;
    }
    memcpy(want_u16, u16, n * sizeof(*u16));
    memcpy(want_i64, i64, n * sizeof(*i64));
    memcpy(want_row, row, n * sizeof(*row));
    qsort(want_u16, n, sizeof(*want_u16), compare_u16);
    qsort(want_i64, n, sizeof(*want_i64), compare_i64);
    qsort(want_row, n, sizeof(*want_row), compare_row);
    size_t expected = n > 256;
    allocations = 0;
    CHECK(ks_introsort(uint16_t, n, u16) == 0 && allocations == expected);
    allocations = 0;
    CHECK(ks_introsort(int64_t, n, i64) == 0 && allocations == expected);
    allocations = 0;
    CHECK(ks_introsort(row, n, row) == 0 && allocations == expected);
    CHECK(memcmp(u16, want_u16, n * sizeof(*u16)) == 0);
    CHECK(memcmp(i64, want_i64, n * sizeof(*i64)) == 0);
    CHECK(memcmp(row, want_row, n * sizeof(*row)) == 0);
    free(u16); free(want_u16); free(i64); free(want_i64); free(row); free(want_row);
}

int main(void) {
    size_t cases = 0;
    const size_t large[] = {511, 512, 513, 1023, 1024, 1025, 65535, 65536, 65537};
    for (size_t n = 0; n <= 260; n++)
        for (int pattern = 0; pattern < 7; pattern++, cases += 3)
            check(n, pattern);
    for (size_t i = 0; i < sizeof(large) / sizeof(*large); i++)
        for (int pattern = 0; pattern < 7; pattern++, cases += 3)
            check(large[i], pattern);
    printf("Introsort: %zu qsort-equivalence cases and allocation boundaries passed\n", cases);
    return 0;
}
