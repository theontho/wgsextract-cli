/* Wire memory so that a benchmark sees a Mac with less RAM.
 * Usage: balloon <MiB>
 * Allocates and mlock()s <MiB> in 64 MiB pieces, prints "wired <n> MiB" (or
 * how far it got and why it stopped), then holds the memory until it receives
 * SIGTERM or SIGINT. Exits 1 if it could not wire everything. */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static volatile sig_atomic_t stop;

static void on_signal(int signo) {
    (void)signo;
    stop = 1;
}

int main(int argc, char **argv) {
    const size_t piece = (size_t)64 << 20;
    char *end;
    long mib = argc == 2 ? strtol(argv[1], &end, 10) : 0;
    if (argc != 2 || *end || mib <= 0) {
        fprintf(stderr, "usage: balloon <MiB>\n");
        return 2;
    }
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    size_t wanted = (size_t)mib << 20, wired = 0;
    while (wired < wanted) {
        size_t size = wanted - wired < piece ? wanted - wired : piece;
        void *block = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
        if (block == MAP_FAILED) {
            fprintf(stderr, "balloon: mmap failed after %zu MiB: %s\n", wired >> 20, strerror(errno));
            break;
        }
        memset(block, 1, size);
        if (mlock(block, size) != 0) {
            fprintf(stderr, "balloon: mlock failed after %zu MiB: %s\n", wired >> 20, strerror(errno));
            munmap(block, size);
            break;
        }
        wired += size;
    }
    printf("wired %zu MiB\n", wired >> 20);
    fflush(stdout);
    while (!stop) pause();
    return wired == wanted ? 0 : 1;
}
