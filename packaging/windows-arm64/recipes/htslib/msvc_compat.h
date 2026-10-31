#ifndef WGSE_HTSLIB_ARM64_MSVC_COMPAT_H
#define WGSE_HTSLIB_ARM64_MSVC_COMPAT_H
#include "../bwa/msvc_compat.h"
#include "../../compat/msvc_files.h"
#include "../../compat/msvc_format.h"
#include <time.h>
#include <stdio.h>
#include <io.h>
#include <stdlib.h>
#include <windows.h>
#include <errno.h>
#include <signal.h>
#include <direct.h>
#ifndef SSIZE_MAX
#define SSIZE_MAX INTPTR_MAX
#endif
#define R_OK 4
#define W_OK 2
#define F_OK 0
#define S_IRUSR _S_IREAD
#define S_IWUSR _S_IWRITE
#define stat _stat64
#define fstat _fstat64
#define S_ISDIR(mode) (((mode) & _S_IFMT) == _S_IFDIR)
#define mkdir(path, mode) _mkdir(path)
#define getcwd _getcwd
static inline int nanosleep(const struct timespec *request, struct timespec *remaining)
{
    if (!request || request->tv_sec < 0 || request->tv_nsec < 0 ||
        request->tv_nsec >= 1000000000) {
        errno = EINVAL;
        return -1;
    }
    HANDLE timer = CreateWaitableTimerExW(NULL, NULL,
        CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    if (!timer) { errno = EIO; return -1; }
    LARGE_INTEGER due;
    due.QuadPart = -(request->tv_sec * 10000000 + (request->tv_nsec + 99) / 100);
    if (!due.QuadPart) {
        CloseHandle(timer);
        if (remaining) memset(remaining, 0, sizeof(*remaining));
        return 0;
    }
    BOOL started = SetWaitableTimer(timer, &due, 0, NULL, NULL, FALSE);
    DWORD waited = started ? WaitForSingleObject(timer, INFINITE) : WAIT_FAILED;
    CloseHandle(timer);
    if (waited != WAIT_OBJECT_0) { errno = EIO; return -1; }
    if (remaining) memset(remaining, 0, sizeof(*remaining));
    return 0;
}
#define fseeko _fseeki64
#define ftello _ftelli64
#define strtok_r strtok_s
static inline int wgse_ftruncate(int fd, wgse_off_t length)
{
    if (fd < 0 || length < 0) {
        errno = fd < 0 ? EBADF : EINVAL;
        return -1;
    }
    errno_t result = _chsize_s(fd, length);
    if (result) { errno = result; return -1; }
    return 0;
}
#define ftruncate wgse_ftruncate
#define localtime_r(value, result) (localtime_s((result), (value)) ? NULL : (result))
#define gmtime_r(value, result) (gmtime_s((result), (value)) ? NULL : (result))
#define realpath(path, result) _fullpath((result), (path), _MAX_PATH)
#endif
