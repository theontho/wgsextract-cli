#pragma once

#include "bwamem.h"

#ifdef __cplusplus
extern "C" {
#endif

void *wgse_seed_scratch_create(void);
void wgse_seed_scratch_destroy(void *scratch);
const bwtintv_v *wgse_seed_collect(const mem_opt_t *options, const bwt_t *bwt,
    int length, const uint8_t *sequence, void *scratch);

#ifdef __cplusplus
}
#endif
