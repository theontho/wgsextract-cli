#ifndef WGSE_FASTP_ARM64_MSVC_COMPAT_H
#define WGSE_FASTP_ARM64_MSVC_COMPAT_H
#include "../htslib/msvc_compat.h"
#include <windows.h>
#include <direct.h>
#define fseeko _fseeki64
#define ftello _ftelli64
#define stat _stat64
#define fstat _fstat64
#define sleep(seconds) Sleep((DWORD)(seconds) * 1000)
#define mkdir(path, mode) _mkdir(path)
static inline int wgse_usleep(unsigned long microseconds)
{
    struct timespec request;
    request.tv_sec = microseconds / 1000000;
    request.tv_nsec = (microseconds % 1000000) * 1000;
    return nanosleep(&request, NULL);
}
#define usleep wgse_usleep
#endif
