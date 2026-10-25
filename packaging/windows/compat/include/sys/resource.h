/* getrusage(RUSAGE_SELF) for MinGW-w64: CPU times and peak memory from the
 * Windows process APIs, so the tools' timing and memory reports stay real. */
#ifndef WGSE_SYS_RESOURCE_H
#define WGSE_SYS_RESOURCE_H

#include <sys/time.h>

#ifdef __cplusplus
extern "C" {
#endif

#define RUSAGE_SELF 0
#define RUSAGE_CHILDREN (-1)

struct rusage {
	struct timeval ru_utime;
	struct timeval ru_stime;
	long ru_maxrss; /* KiB, as on Linux */
	long ru_minflt, ru_majflt, ru_nvcsw, ru_nivcsw;
};

int getrusage(int who, struct rusage *usage);

#ifdef __cplusplus
}
#endif
#endif
