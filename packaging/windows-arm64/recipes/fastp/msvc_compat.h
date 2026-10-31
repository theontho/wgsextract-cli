#ifndef WGSE_FASTP_ARM64_MSVC_COMPAT_H
#define WGSE_FASTP_ARM64_MSVC_COMPAT_H
#include "../bwa/msvc_compat.h"
#include "../../compat/msvc_format.h"
#include <windows.h>
#include <direct.h>
#define fseeko _fseeki64
#define ftello _ftelli64
#define stat _stat64
#define fstat _fstat64
#define sleep(seconds) Sleep((DWORD)(seconds) * 1000)
#define mkdir(path, mode) _mkdir(path)
#endif
