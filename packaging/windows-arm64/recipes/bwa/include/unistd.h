#ifndef WGSE_ARM64_UNISTD_H
#define WGSE_ARM64_UNISTD_H

#include <io.h>
#include <process.h>
#include <getopt.h>
#define STDIN_FILENO 0
#define STDOUT_FILENO 1
#define STDERR_FILENO 2
#define lseek _lseeki64

#endif
