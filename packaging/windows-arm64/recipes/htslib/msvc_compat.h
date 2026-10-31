#ifndef WGSE_HTSLIB_ARM64_MSVC_COMPAT_H
#define WGSE_HTSLIB_ARM64_MSVC_COMPAT_H
#include "../bwa/msvc_compat.h"
#include <time.h>
#include <stdio.h>
#include <io.h>
#include <stdlib.h>
#define fseeko _fseeki64
#define ftello _ftelli64
#define strtok_r strtok_s
#define ftruncate(fd, length) _chsize_s((fd), (length))
#define localtime_r(value, result) (localtime_s((result), (value)) ? NULL : (result))
#define gmtime_r(value, result) (gmtime_s((result), (value)) ? NULL : (result))
#define realpath(path, result) _fullpath((result), (path), _MAX_PATH)
#endif
