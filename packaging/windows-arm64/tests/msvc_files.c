#include <assert.h>
#include <stdio.h>
#include <sys/stat.h>
#include "msvc_files.h"

int main(void)
{
    char invalid[] = "invalid";
    assert(mkstemp(invalid) == -1 && errno == EINVAL);
    char first[] = "out/native-files-XXXXXX";
    char second[] = "out/native-files-XXXXXX";
    int one = mkstemp(first), two = mkstemp(second);
    assert(one >= 0 && two >= 0 && strcmp(first, second));
    assert(_write(one, "A\nB\n", 4) == 4);
    assert(ftruncate(one, -1) == -1 && errno == EINVAL);
    assert(ftruncate(one, 2) == 0);
    assert(_close(one) == 0 && _close(two) == 0);
    struct _stat64 info;
    assert(_stat64(first, &info) == 0 && info.st_size == 2);
    assert(_unlink(first) == 0 && _unlink(second) == 0);
    puts("PASS: native atomic unique files, writable binary descriptors and cleanup");
    return 0;
}
