/* Stands in for safestringlib's safe_mem_lib.h on Windows: the UCRT already has the
 * bounds-checked functions bwa-mem2 uses (memcpy_s, strcpy_s, strncpy_s,
 * strcat_s, strncat_s) with the same signatures, and safestringlib's own
 * definitions conflict with them. */
#ifndef WGSE_SAFESTRING_STUB_H
#define WGSE_SAFESTRING_STUB_H
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* safestringlib's limits, which bwa-mem2 copies in chunks of. */
#define RSIZE_MAX_MEM (256UL << 20)
#define RSIZE_MAX_STR (4UL << 10)
#endif
