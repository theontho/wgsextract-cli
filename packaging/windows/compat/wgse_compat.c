/* Implementations for wgse_compat.h and sys/resource.h. */
#include <windows.h>
#include <psapi.h>
#include <errno.h>
#include <fcntl.h>
#include <io.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "wgse_compat.h"
#include "sys/mman.h"
#include "sys/resource.h"
#include "sys/times.h"

/* Binary I/O, as on Unix: stdin/stdout/stderr and files opened without "b"
 * or "t" don't rewrite \n as \r\n (UCRT's binmode.o is empty, so it can't
 * do this). Runs before main(). */
__attribute__((constructor)) static void wgse_binary_io(void)
{
	_set_fmode(_O_BINARY);
	_setmode(0, _O_BINARY);
	_setmode(1, _O_BINARY);
	_setmode(2, _O_BINARY);
}

/* POSIX drand48: X(n+1) = (a * X(n) + c) mod 2^48. */
static uint64_t wgse_rand48_state = 0x1234ABCD330EULL;

static uint64_t wgse_rand48_next(void)
{
	wgse_rand48_state = (0x5DEECE66DULL * wgse_rand48_state + 0xBULL) & 0xFFFFFFFFFFFFULL;
	return wgse_rand48_state;
}

double wgse_drand48(void) { return (double)wgse_rand48_next() / 281474976710656.0; }
long wgse_lrand48(void) { return (long)(wgse_rand48_next() >> 17); }
long wgse_mrand48(void) { return (long)(int32_t)(wgse_rand48_next() >> 16); }

void wgse_srand48(long seed)
{
	wgse_rand48_state = (((uint64_t)(uint32_t)seed) << 16 | 0x330EULL) & 0xFFFFFFFFFFFFULL;
}

static uint64_t filetime_100ns(const FILETIME *ft)
{
	return (uint64_t)ft->dwHighDateTime << 32 | ft->dwLowDateTime;
}

static void filetime_to_timeval(const FILETIME *ft, struct timeval *tv)
{
	uint64_t t = filetime_100ns(ft) / 10; /* 100 ns -> us */
	tv->tv_sec = (long)(t / 1000000);
	tv->tv_usec = (long)(t % 1000000);
}

clock_t times(struct tms *buffer)
{
	FILETIME created, exited, kernel, user;
	if (buffer) {
		memset(buffer, 0, sizeof(*buffer));
		if (GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user)) {
			buffer->tms_utime = (clock_t)(filetime_100ns(&user) * CLOCKS_PER_SEC / 10000000);
			buffer->tms_stime = (clock_t)(filetime_100ns(&kernel) * CLOCKS_PER_SEC / 10000000);
		}
	}
	return clock();
}

void *mmap(void *addr, size_t length, int prot, int flags, int fd, off_t offset)
{
	void *p;
	(void)addr;
	(void)fd;
	(void)offset;
	if (!(flags & MAP_ANONYMOUS)) {
		errno = ENOSYS;
		return MAP_FAILED;
	}
	p = VirtualAlloc(NULL, length, MEM_RESERVE | MEM_COMMIT,
			 prot & PROT_WRITE ? PAGE_READWRITE : PAGE_READONLY);
	if (!p) {
		errno = ENOMEM;
		return MAP_FAILED;
	}
	return p;
}

int munmap(void *addr, size_t length)
{
	(void)length;
	if (!VirtualFree(addr, 0, MEM_RELEASE)) {
		errno = EINVAL;
		return -1;
	}
	return 0;
}

int getrusage(int who, struct rusage *usage)
{
	FILETIME created, exited, kernel, user;
	PROCESS_MEMORY_COUNTERS mem;
	if (who != RUSAGE_SELF) {
		errno = EINVAL;
		return -1;
	}
	memset(usage, 0, sizeof(*usage));
	if (GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user)) {
		filetime_to_timeval(&user, &usage->ru_utime);
		filetime_to_timeval(&kernel, &usage->ru_stime);
	}
	if (GetProcessMemoryInfo(GetCurrentProcess(), &mem, sizeof(mem))) {
		usage->ru_maxrss = (long)(mem.PeakWorkingSetSize / 1024);
		usage->ru_majflt = (long)mem.PageFaultCount;
	}
	return 0;
}

static OVERLAPPED at_offset(off_t offset)
{
	OVERLAPPED o;
	memset(&o, 0, sizeof(o));
	o.Offset = (DWORD)((uint64_t)offset & 0xFFFFFFFFu);
	o.OffsetHigh = (DWORD)((uint64_t)offset >> 32);
	return o;
}

/* One call moves at most 1 GiB (ReadFile/WriteFile take a DWORD count);
 * callers loop on short counts, as POSIX allows. */
#define WGSE_IO_CHUNK ((size_t)1 << 30)

ssize_t pread(int fd, void *buf, size_t count, off_t offset)
{
	HANDLE h = (HANDLE)_get_osfhandle(fd);
	OVERLAPPED o = at_offset(offset);
	DWORD done = 0;
	if (h == INVALID_HANDLE_VALUE) {
		errno = EBADF;
		return -1;
	}
	if (!ReadFile(h, buf, (DWORD)(count < WGSE_IO_CHUNK ? count : WGSE_IO_CHUNK), &done, &o)) {
		if (GetLastError() == ERROR_HANDLE_EOF) return 0;
		errno = EIO;
		return -1;
	}
	return (ssize_t)done;
}

ssize_t pwrite(int fd, const void *buf, size_t count, off_t offset)
{
	HANDLE h = (HANDLE)_get_osfhandle(fd);
	OVERLAPPED o = at_offset(offset);
	DWORD done = 0;
	if (h == INVALID_HANDLE_VALUE) {
		errno = EBADF;
		return -1;
	}
	if (!WriteFile(h, buf, (DWORD)(count < WGSE_IO_CHUNK ? count : WGSE_IO_CHUNK), &done, &o)) {
		errno = GetLastError() == ERROR_DISK_FULL ? ENOSPC : EIO;
		return -1;
	}
	return (ssize_t)done;
}

ssize_t getline(char **lineptr, size_t *n, FILE *stream)
{
	size_t len = 0;
	int c;
	if (!lineptr || !n || !stream) {
		errno = EINVAL;
		return -1;
	}
	if (!*lineptr || *n == 0) {
		*n = 256;
		if (!(*lineptr = (char *)malloc(*n))) return -1;
	}
	while ((c = getc(stream)) != EOF) {
		if (len + 1 >= *n) {
			size_t size = *n * 2;
			char *grown = (char *)realloc(*lineptr, size);
			if (!grown) return -1;
			*lineptr = grown;
			*n = size;
		}
		(*lineptr)[len++] = (char)c;
		if (c == '\n') break;
	}
	if (len == 0) return -1;
	(*lineptr)[len] = '\0';
	return (ssize_t)len;
}
