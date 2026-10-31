#ifndef WGSE_MINIMAP2_UNISTD_H
#define WGSE_MINIMAP2_UNISTD_H
#include <io.h>
#include <process.h>
#define STDIN_FILENO 0
#define STDOUT_FILENO 1
#define STDERR_FILENO 2
#define lseek _lseeki64
#endif
