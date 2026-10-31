#include <windows.h>
#include <errno.h>
#include <sys/time.h>

int gettimeofday(struct timeval *tv, struct timezone *tz)
{
    FILETIME time;
    ULARGE_INTEGER ticks;
    (void)tz;
    if (!tv) { errno = EINVAL; return -1; }
    GetSystemTimePreciseAsFileTime(&time);
    ticks.LowPart = time.dwLowDateTime;
    ticks.HighPart = time.dwHighDateTime;
    ticks.QuadPart -= 116444736000000000ULL;
    tv->tv_sec = (long)(ticks.QuadPart / 10000000);
    tv->tv_usec = (long)((ticks.QuadPart % 10000000) / 10);
    return 0;
}
