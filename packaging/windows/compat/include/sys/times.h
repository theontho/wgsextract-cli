/* times() for MinGW-w64, from the Windows process times. */
#ifndef WGSE_SYS_TIMES_H
#define WGSE_SYS_TIMES_H

#include <time.h>

#ifdef __cplusplus
extern "C" {
#endif

struct tms {
	clock_t tms_utime;
	clock_t tms_stime;
	clock_t tms_cutime;
	clock_t tms_cstime;
};

/* Times are in CLOCKS_PER_SEC units; returns the elapsed time since start. */
clock_t times(struct tms *buffer);

#ifdef __cplusplus
}
#endif
#endif
