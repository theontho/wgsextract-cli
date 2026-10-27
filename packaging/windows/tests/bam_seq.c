/* Check bam_set1's packed sequence against the original byte lookup table. */
#include "htslib/sam.h"
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

static void check(bam1_t *bam, const unsigned char *seq, size_t length,
                  const char *name) {
    CHECK(bam_set1(bam, strlen(name), name, BAM_FUNMAP, -1, 0, 0, 0, NULL,
                   -1, 0, 0, length, (const char *)seq, NULL, 0) >= 0);
    CHECK(bam->core.l_qseq == (int32_t)length);
    CHECK(strcmp(bam_get_qname(bam), name) == 0);
    const uint8_t *packed = bam_get_seq(bam);
    const uint8_t *quality = bam_get_qual(bam);
    for (size_t i = 0; i < length; i++) {
        CHECK(bam_seqi(packed, i) == seq_nt16_table[seq[i]]);
        CHECK(quality[i] == 255);
    }
    if (length & 1)
        CHECK((packed[length / 2] & 15) == 0);
}

int main(void) {
    static const char *names[] = {"r", "qname", "long-query", "long-query-name"};
    static const size_t lengths[] = {
        0, 1, 2, 15, 16, 17, 31, 32, 33, 63, 64, 65,
        149, 150, 151, 255, 256, 257, 4096
    };
    static const char alphabet[] = "ACMGRSVTWYHKDBNacmgrsvtwyhkdbn";
    bam1_t *bam = bam_init1();
    CHECK(bam);
    unsigned char *pair_storage = malloc(40);
    CHECK(pair_storage);
    unsigned char *pair = pair_storage + 7;
    memset(pair, 'A', 33);
    size_t cases = 0;
    for (unsigned a = 0; a < 256; a++)
        for (unsigned b = 0; b < 256; b++) {
            pair[0] = a;
            pair[1] = b;
            check(bam, pair, 33, names[a & 3]);
            cases++;
        }
    free(pair_storage);
    for (size_t l = 0; l < sizeof(lengths) / sizeof(*lengths); l++)
        for (size_t offset = 0; offset < 16; offset++)
            for (size_t name = 0; name < sizeof(names) / sizeof(*names); name++)
                for (int pattern = 0; pattern < 3; pattern++) {
                    size_t length = lengths[l];
                    unsigned char *storage = malloc(length + offset + 1);
                    CHECK(storage);
                    unsigned char *seq = storage + offset;
                    for (size_t i = 0; i < length; i++)
                        seq[i] = pattern == 0 ? 'A' : pattern == 1
                            ? (unsigned char)alphabet[i % (sizeof(alphabet) - 1)]
                            : (unsigned char)(i * 131 + offset * 17);
                    check(bam, seq, length, names[name]);
                    cases++;
                    free(storage);
                }
    bam_destroy1(bam);
    printf("BAM sequence packing: %zu scalar-equivalence cases passed\n", cases);
    return 0;
}
