#include <assert.h>
#include <string.h>
#include "../compat/msvc_format.h"

int main(void)
{
    char *text = NULL;
    int count = wgse_asprintf(&text, "%s:%llu:%.*s", "chr1",
        139000000000ULL, 4, "ACGTN");
    assert(text && count == (int)strlen("chr1:139000000000:ACGT"));
    assert(strcmp(text, "chr1:139000000000:ACGT") == 0);
    free(text);
    count = wgse_asprintf(&text, "%s", "");
    assert(count == 0 && text && text[0] == '\0');
    free(text);
    return 0;
}
