#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "bwt.h"

#define CHECK(expression) do { \
	if (!(expression)) { \
		fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expression); \
		exit(1); \
	} \
} while (0)

static uint32_t random_word(uint32_t *state)
{
	*state = *state * UINT32_C(1664525) + UINT32_C(1013904223);
	return *state;
}

static void write_reference(const char *path)
{
	FILE *file = fopen(path, "wb");
	uint32_t state = 17;
	CHECK(file != NULL);
	CHECK(fputs(">test\n", file) >= 0);
	for (size_t i = 0; i < 32341; ++i) {
		int base = "ACGT"[(random_word(&state) >> 16) & 3];
		if (i >= 16000 && i < 20000) base = "AACCGGTTAACCGGTT"[i & 15];
		if (i % 97 == 0) base = 'N';
		CHECK(fputc(base, file) != EOF);
		if (i % 60 == 59) CHECK(fputc('\n', file) != EOF);
	}
	CHECK(fputc('\n', file) != EOF);
	CHECK(fclose(file) == 0);
}

int main(int argc, char **argv)
{
	bwt_t *bwt;
	bwtint_t *input, *expected, *actual;
	const size_t chunks[] = {1, 2, 3, 4, 5, 7, 31, 257, 4096};
	uint32_t state = 29;
	size_t n;
	CHECK(argc == 3);
	if (strcmp(argv[1], "--write-ref") == 0) {
		write_reference(argv[2]);
		return 0;
	}
	bwt = bwt_restore_bwt(argv[1]);
	CHECK(bwt != NULL && bwt->seq_len > 0 && bwt->seq_len < 1000000);
	bwt_restore_sa(argv[2], bwt);
	n = (size_t)bwt->seq_len + 1;
	input = malloc(n * sizeof(*input));
	expected = malloc(n * sizeof(*expected));
	actual = malloc(n * sizeof(*actual));
	CHECK(input != NULL && expected != NULL && actual != NULL);
	for (int interval = 1; interval <= 128; interval *= 2) {
		bwt_cal_sa(bwt, interval);
		for (size_t i = 0; i < n; ++i) input[i] = i;
		bwt_sa_many(bwt, 0, NULL);
		for (int trial = 0; trial < 3; ++trial) {
			for (size_t i = 0; i < n; ++i) expected[i] = bwt_sa(bwt, input[i]);
			for (size_t c = 0; c < sizeof(chunks) / sizeof(chunks[0]); ++c) {
				memcpy(actual, input, n * sizeof(*input));
				for (size_t start = 0; start < n; start += chunks[c]) {
					size_t count = n - start < chunks[c]? n - start : chunks[c];
					bwt_sa_many(bwt, count, actual + start);
				}
				for (size_t i = 0; i < n; ++i) CHECK(actual[i] == expected[i]);
			}
			for (size_t i = n - 1; i > 0; --i) {
				size_t j = random_word(&state) % (i + 1);
				bwtint_t value = input[i];
				input[i] = input[j]; input[j] = value;
			}
		}
	}
	free(actual); free(expected); free(input);
	bwt_destroy(bwt);
	puts("Batched suffix-array lookup matched every scalar result across orders and chunk sizes");
	return 0;
}
