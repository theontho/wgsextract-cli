/* Real group_smpl bodies, real pileup layout and real sample selection. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <htslib/sam.h>
#include "bam_sample.h"
#include "bam2bcf.h"

#define CHECK(expression) do { \
    if (!(expression)) { \
        fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expression); \
        exit(1); \
    } \
} while (0)

/* bam_sample.c diagnostic dependency: malformed fixtures must fail. */
void error(const char *format, ...) {
    va_list args;
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    exit(1);
}

typedef struct {
    int n;
    int *n_plp, *m_plp;
    bam_pileup1_t **plp;
} mplp_pileup_t;
#define group_smpl scalar_group
#include "group_original.inc"
#undef group_smpl
#include "group_candidate.inc"

static mplp_pileup_t create(int samples) {
    mplp_pileup_t m = {0};
    m.n = samples;
    m.n_plp = calloc(samples, sizeof(*m.n_plp));
    m.m_plp = calloc(samples, sizeof(*m.m_plp));
    m.plp = calloc(samples, sizeof(*m.plp));
    CHECK(m.n_plp && m.m_plp && m.plp);
    return m;
}

static void destroy(mplp_pileup_t *m) {
    for (int i = 0; i < m->n; i++) free(m->plp[i]);
    free(m->plp); free(m->m_plp); free(m->n_plp);
}

static void compare(mplp_pileup_t *expected, mplp_pileup_t *actual,
                    int files, int *counts, const bam_pileup1_t **pileups) {
    scalar_group(expected, NULL, files, counts, pileups);
    group_smpl(actual, NULL, files, counts, pileups);
    for (int i = 0; i < actual->n; i++) {
        CHECK(expected->n_plp[i] == actual->n_plp[i]);
        CHECK(expected->m_plp[i] == actual->m_plp[i]);
        if (actual->n_plp[i])
            CHECK(memcmp(expected->plp[i], actual->plp[i],
                         actual->n_plp[i] * sizeof(bam_pileup1_t)) == 0);
    }
}

static void boundaries(int files, int samples) {
    const int sizes[] = {0, 1, 7, 8, 9, 15, 16, 17, 257, 1, 0};
    bam_pileup1_t data[2][257] = {{{0}}};
    plp_cd_t metadata[2][257] = {{{0}}};
    const bam_pileup1_t *pileups[] = {data[0], data[1]};
    for (int file = 0; file < files; file++)
        for (int j = 0; j < 257; j++) {
            data[file][j].qpos = j + file * 300;
            data[file][j].indel = j % 5 - 2;
            data[file][j].is_del = j & 1;
            data[file][j].cd.p = &metadata[file][j];
            PLP_SET_SAMPLE_ID(&data[file][j].cd, (j + file) % samples);
        }
    mplp_pileup_t expected = create(samples), actual = create(samples);
    for (size_t i = 0; i < sizeof(sizes) / sizeof(*sizes); i++) {
        int counts[] = {sizes[i], sizes[i] / 2};
        compare(&expected, &actual, files, counts, pileups);
    }
    destroy(&expected); destroy(&actual);
}

static void selected_read_group(void) {
    bam_smpl_t *samples = bam_smpl_init();
    CHECK(samples);
    char selected[] = "keep";
    CHECK(bam_smpl_add_samples(samples, selected, 0) >= 0);
    char header[] = "@HD\tVN:1.6\n@RG\tID:yes\tSM:keep\n@RG\tID:no\tSM:drop\n";
    CHECK(bam_smpl_add_bam(samples, header, "test.bam") == 0);
    int count;
    bam_smpl_get_samples(samples, &count);
    CHECK(count == 1);
    bam_pileup1_t pileup[2] = {{0}};
    plp_cd_t metadata[2] = {{0}};
    bam1_t *records[2] = {bam_init1(), bam_init1()};
    CHECK(records[0] && records[1]);
    const char *groups[] = {"yes", "no"};
    int retained = 0;
    for (int i = 0; i < 2; i++) {
        CHECK(bam_set1(records[i], 1, "r", BAM_FUNMAP, -1, 0, 0, 0,
                       NULL, -1, 0, 0, 0, NULL, NULL, 0) >= 0);
        CHECK(bam_aux_append(records[i], "RG", 'Z', strlen(groups[i]) + 1,
                             (const uint8_t *)groups[i]) == 0);
        int id = bam_smpl_get_sample_id(samples, 0, records[i]);
        CHECK(id == (i == 0 ? 0 : -1));
        /* mpileup_read rejects negative IDs before calling group_smpl. */
        if (id < 0) continue;
        pileup[retained].b = records[i];
        pileup[retained].cd.p = &metadata[retained];
        PLP_SET_SAMPLE_ID(&pileup[retained].cd, id);
        retained++;
    }
    CHECK(retained == 1);
    const bam_pileup1_t *pileups[] = {pileup};
    mplp_pileup_t expected = create(1), actual = create(1);
    compare(&expected, &actual, 1, &retained, pileups);
    CHECK(actual.n_plp[0] == 1 && actual.plp[0][0].b == records[0]);
    destroy(&expected); destroy(&actual);
    bam_destroy1(records[0]); bam_destroy1(records[1]);
    bam_smpl_destroy(samples);
}

int main(void) {
    boundaries(1, 1);
    boundaries(1, 2);
    boundaries(2, 1);
    boundaries(2, 2);
    selected_read_group();
    puts("group_smpl: growth, empty/reused state, sample/file fallback and RG selection passed");
    return 0;
}
