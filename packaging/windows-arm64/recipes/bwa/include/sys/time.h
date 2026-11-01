#ifndef WGSE_ARM64_SYS_TIME_H
#define WGSE_ARM64_SYS_TIME_H

#include <winsock2.h>
struct timezone { int tz_minuteswest, tz_dsttime; };
#ifdef __cplusplus
extern "C" {
#endif
int gettimeofday(struct timeval *tv, struct timezone *tz);
#ifdef __cplusplus
}
#endif

#endif
