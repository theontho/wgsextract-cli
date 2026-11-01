#include "wgse_index_io.h"
#include "kstring.h"
#include "utils.h"

#include <cassert>
#include <cstring>
#include <sstream>
#include <string>

#ifdef _WIN32
#include <windows.h>
#include <winioctl.h>
#include <io.h>
#endif

static void write_reference(const char *path)
{
    FILE *file = fopen(path, "wb");
    assert(file);
    uint32_t state = 23;
    const char *names[] = {"chr1", "chrM"};
    const size_t lengths[] = {20000, 1024};
    for (size_t contig = 0; contig < 2; ++contig) {
        fprintf(file, ">%s\n", names[contig]);
        for (size_t i = 0; i < lengths[contig]; ++i) {
            state = state * UINT32_C(1664525) + UINT32_C(1013904223);
            const char base = i % 113 == 0 ? 'N' : "ACGT"[(state >> 24) & 3];
            assert(fputc(base, file) != EOF);
            if ((i + 1) % 60 == 0)
                assert(fputc('\n', file) != EOF);
        }
        if (lengths[contig] % 60)
            assert(fputc('\n', file) != EOF);
    }
    assert(fclose(file) == 0);
}

int main(int argc, char **argv)
{
    assert(argc == 3);
    FILE *file = fopen(argv[1], "w+b");
    assert(file);
    const std::string payload = "binary\npayload\x1a-with-element-boundaries";
    assert(wgse_index_write(payload.data(), 1, payload.size(), file, 7)
           == payload.size());
    err_fseek(file, 0, SEEK_SET);
    char buffer[64] = {};
    assert(wgse_index_read(buffer, 4, 16, file, 3) == payload.size() / 4);
    assert(std::memcmp(buffer, payload.data(), payload.size()) == 0);
    assert(feof(file));
    clearerr(file);
    errno = 0;
    assert(wgse_index_write(NULL, SIZE_MAX, 2, file, 7) == 0);
    assert(errno == EOVERFLOW);

    std::ostringstream stream;
    wgse_index_stream_write(stream, payload.data(), payload.size(), 5);
    wgse_index_stream_flush(stream);
    assert(stream.str() == payload);
    stream.setstate(std::ios::badbit);
    bool failed = false;
    try {
        wgse_index_stream_write(stream, payload.data(), payload.size(), 5);
    } catch (const std::ios_base::failure &) {
        failed = true;
    }
    assert(failed);

#ifdef _WIN32
    const HANDLE handle = reinterpret_cast<HANDLE>(_get_osfhandle(_fileno(file)));
    DWORD returned = 0;
    assert(DeviceIoControl(handle, FSCTL_SET_SPARSE, NULL, 0, NULL, 0,
                           &returned, NULL));
#endif
    const int64_t offset = (INT64_C(1) << 32) + 127;
    err_fseek(file, offset, SEEK_SET);
    const char tail[] = "tail";
    assert(err_fwrite(tail, 1, sizeof(tail), file) == sizeof(tail));
    assert(err_ftell(file) == offset + sizeof(tail));
    err_fseek(file, offset, SEEK_SET);
    char result[sizeof(tail)] = {};
    assert(err_fread_noeof(result, 1, sizeof(result), file) == sizeof(result));
    assert(std::memcmp(result, tail, sizeof(tail)) == 0);
    assert(fclose(file) == 0);
    assert(remove(argv[1]) == 0);

    kstring_t number = {0, 0, NULL};
    assert(kputl(INT64_C(6434693834), &number) >= 0);
    assert(std::string(number.s) == "6434693834");
    free(number.s);
    write_reference(argv[2]);
    return 0;
}
