#pragma once

#include "ksw.h"

#ifdef __cplusplus
extern "C" {
#endif

// ksw_align2() for n tasks at once with upstream's exact results (ksw_oracle.c.in),
// for wgse-bwa and wgse-bwa-mem2.
typedef struct {
    int qlen, tlen, xtra;
    uint8_t *query, *target;
    kswr_t r;
} wgse_ksw_task_t;
void wgse_ksw_align2_batch(int n, wgse_ksw_task_t *tasks, int m, const int8_t *mat,
    int o_del, int e_del, int o_ins, int e_ins);

#ifdef __cplusplus
}
#endif
