#ifndef WGSE_MINIMAP2_SYS_TIME_H
#define WGSE_MINIMAP2_SYS_TIME_H
#include <winsock2.h>
struct timezone;
int gettimeofday(struct timeval *value, struct timezone *zone);
#endif
