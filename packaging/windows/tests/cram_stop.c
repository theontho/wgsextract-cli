/* Compare the real CRAM stop codec with HTSlib 1.24's scalar implementation. */
#include "config.h"
#include "cram/cram.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int cram_byte_array_stop_decode_block(cram_slice *, cram_codec *, cram_block *,
                                      char *, int *);

static int scalar(cram_slice *slice, cram_codec *c, cram_block *in,
                  char *out_, int *out_size) {
    (void)in;
    cram_block *b = cram_get_block_by_id(slice, c->u.byte_array_stop.content_id);
    cram_block *out = (cram_block *)out_;
    if (!b)
        return *out_size ? -1 : 0;
    if (b->idx >= b->uncomp_size)
        return -1;
    unsigned char *cp = b->data + b->idx;
    unsigned char *cp_end = b->data + b->uncomp_size;
    unsigned char stop = b->orig_method == TOK3 ? 0 : c->u.byte_array_stop.stop;
    if (cp_end - cp < out->alloc - out->byte) {
        unsigned char *out_cp = BLOCK_END(out);
        while (cp != cp_end && *cp != stop)
            *out_cp++ = *cp++;
        BLOCK_SIZE(out) = out_cp - BLOCK_DATA(out);
    } else {
        unsigned char *cp_start;
        for (cp_start = cp; cp != cp_end && *cp != stop; cp++)
            ;
        BLOCK_APPEND(out, cp_start, cp - cp_start);
        BLOCK_GROW(out, cp - cp_start);
    }
    *out_size = cp - (b->data + b->idx);
    b->idx = cp - b->data + 1;
    return 0;
block_err:
    return -1;
}

static void compare(size_t length, unsigned stop, int tokenized, int placement,
                    size_t prefix, int capacity_case, int offset) {
    unsigned actual_stop = tokenized ? 0 : stop;
    size_t size = length + offset;
    unsigned char *data = malloc(size ? size : 1);
    assert(data);
    memset(data, 0xa5, size);
    for (size_t i = 0; i < length; i++) {
        unsigned value = (i * 131 + stop * 17) & 255;
        data[offset + i] = value == actual_stop ? (value + 1) & 255 : value;
    }
    if (length && placement < 3) {
        size_t position = placement == 0 ? 0 : placement == 1 ? length / 2 : length - 1;
        data[offset + position] = actual_stop;
    }
    size_t capacity = prefix;
    if (capacity_case == 1)
        capacity += length / 2;
    else if (capacity_case == 2)
        capacity += length;
    else if (capacity_case == 3)
        capacity += length + 1;
    cram_block input[2] = {{0}, {0}}, output[2] = {{0}, {0}};
    cram_block *lookup[2][256] = {{0}, {0}};
    cram_slice slice[2] = {{0}, {0}};
    cram_codec codec = {0};
    codec.u.byte_array_stop.content_id = 17;
    codec.u.byte_array_stop.stop = stop;
    for (int i = 0; i < 2; i++) {
        input[i].data = data;
        input[i].idx = offset;
        input[i].uncomp_size = size;
        input[i].orig_method = tokenized ? TOK3 : RAW;
        output[i].data = capacity ? malloc(capacity) : NULL;
        assert(!capacity || output[i].data);
        if (capacity)
            memset(output[i].data, 0xcd, capacity);
        output[i].alloc = capacity;
        output[i].byte = prefix;
        lookup[i][17] = &input[i];
        slice[i].block_by_id = lookup[i];
    }
    /* Repeat calls on the same input and output, including exhaustion. */
    for (int call = 0; call < 4; call++) {
        int sizes[2] = {1, 1};
        int expected = scalar(&slice[0], &codec, NULL, (char *)&output[0], &sizes[0]);
        int actual = cram_byte_array_stop_decode_block(
            &slice[1], &codec, NULL, (char *)&output[1], &sizes[1]);
        assert(actual == expected);
        assert(sizes[0] == sizes[1]);
        assert(input[0].idx == input[1].idx);
        assert(output[0].byte == output[1].byte);
        assert(output[0].alloc == output[1].alloc);
        if (output[0].byte)
            assert(memcmp(output[0].data, output[1].data, output[0].byte) == 0);
    }
    free(output[0].data);
    free(output[1].data);
    free(data);
}

int main(void) {
    static const size_t lengths[] = {0, 1, 2, 15, 16, 17, 63, 64, 65, 255, 256, 257, 4096};
    size_t cases = 0;
    for (unsigned stop = 0; stop < 256; stop++)
        for (int tokenized = 0; tokenized < 2; tokenized++)
            for (size_t i = 0; i < sizeof(lengths) / sizeof(*lengths); i++)
                for (int placement = 0; placement < 4; placement++)
                    for (int capacity = 0; capacity < 4; capacity++)
                        for (int variant = 0; variant < 2; variant++) {
                            compare(lengths[i], stop, tokenized, placement,
                                    variant ? 13 : 0, capacity, variant ? 7 : 0);
                            cases++;
                        }
    cram_block *lookup[256] = {0};
    cram_slice slice = {0};
    cram_codec codec = {0};
    cram_block output = {0};
    slice.block_by_id = lookup;
    codec.u.byte_array_stop.content_id = 17;
    for (int requested = 0; requested < 2; requested++) {
        int size = requested;
        assert(cram_byte_array_stop_decode_block(
            &slice, &codec, NULL, (char *)&output, &size) == (requested ? -1 : 0));
        assert(size == requested);
    }
    printf("CRAM stop codec: %zu scalar-equivalence cases passed\n", cases);
    return 0;
}
