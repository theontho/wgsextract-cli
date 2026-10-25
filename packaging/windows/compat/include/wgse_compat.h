/* POSIX functions that MinGW-w64 (UCRT) lacks, for building Unix tools natively
 * on Windows. Force-included into every translation unit (-include), so it must
 * stay safe to include from both C and C++ and on its own.
 */
#ifndef WGSE_COMPAT_H
#define WGSE_COMPAT_H

#ifdef _WIN32
/* GNU extensions MinGW has (asprintf, vasprintf), as glibc's g++ assumes. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <io.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The drand48 family, with POSIX's exact 48-bit generator, so tools that use
 * it (BWA breaks ties and replaces ambiguous bases with it) give the same
 * output as their Linux and macOS builds. */
double wgse_drand48(void);
long wgse_lrand48(void);
long wgse_mrand48(void);
void wgse_srand48(long seed);
#define drand48 wgse_drand48
#define lrand48 wgse_lrand48
#define mrand48 wgse_mrand48
#define srand48 wgse_srand48

#ifndef __cplusplus
/* index() clashes with C++ member names; C code only. */
#define index(s, c) strchr((s), (c))
#define rindex(s, c) strrchr((s), (c))
#endif

#define fsync(fd) _commit(fd)

/* Positioned I/O on a CRT file descriptor (OVERLAPPED ReadFile/WriteFile).
 * Unlike POSIX, the descriptor's file position may move, so don't mix these
 * with read/write/lseek on the same descriptor. */
ssize_t pread(int fd, void *buf, size_t count, off_t offset);
ssize_t pwrite(int fd, const void *buf, size_t count, off_t offset);

/* A real function, not a macro, so it doesn't hide C++'s std::getline.
 * (MinGW-w64 already has asprintf and vasprintf.) */
ssize_t getline(char **lineptr, size_t *n, FILE *stream);

#ifdef __cplusplus
}
#endif
#endif /* _WIN32 */
#endif /* WGSE_COMPAT_H */
