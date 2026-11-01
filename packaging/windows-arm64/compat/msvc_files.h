#ifndef WGSE_ARM64_MSVC_FILES_H
#define WGSE_ARM64_MSVC_FILES_H
#include <windows.h>
#include <bcrypt.h>
#include <errno.h>
#include <fcntl.h>
#include <io.h>
#include <stdint.h>
#include <string.h>

static inline int wgse_mkstemp(char *path)
{
    static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    size_t length = path ? strlen(path) : 0;
    if (length < 6 || strcmp(path + length - 6, "XXXXXX")) {
        errno = EINVAL;
        return -1;
    }
    for (int attempt = 0; attempt < 128; attempt++) {
        unsigned char random[6];
        if (BCryptGenRandom(NULL, random, sizeof(random), BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) {
            errno = EIO;
            return -1;
        }
        for (int i = 0; i < 6; i++) path[length - 6 + i] = alphabet[random[i] % 62];
        HANDLE file = CreateFileA(path, GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
            CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
        if (file == INVALID_HANDLE_VALUE) {
            DWORD error = GetLastError();
            if (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS) continue;
            if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
                errno = ENOENT;
            else if (error == ERROR_ACCESS_DENIED || error == ERROR_SHARING_VIOLATION)
                errno = EACCES;
            else
                errno = EIO;
            return -1;
        }
        int fd = _open_osfhandle((intptr_t)file, _O_RDWR | _O_BINARY);
        if (fd < 0) {
            CloseHandle(file);
            DeleteFileA(path);
        }
        return fd;
    }
    errno = EEXIST;
    return -1;
}
#define mkstemp wgse_mkstemp
#define popen _popen
#define pclose _pclose
#endif
