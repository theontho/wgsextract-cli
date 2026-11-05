#pragma once

#include "bandedSWA.h"

#include <cstdint>
#include <fstream>
#include <stdexcept>
#include <string>
#include <vector>

struct ExtensionParameters {
    int32_t count, bandwidth, open_del, extend_del, open_ins, extend_ins;
    int32_t zdrop, end_bonus, max_substitution;
    int8_t matrix[25];
    bool simple_matrix;
    uint8_t precision;
};
static_assert(sizeof(ExtensionParameters) == 64);

// Versioned, little-endian benchmark fixture, not a public interchange format.
struct ExtensionBatchHeader {
    uint64_t magic = 0x5747534542415431;
    uint32_t version = 1, pair_size = sizeof(SeqPair);
    ExtensionParameters parameters{};
    uint64_t reference_bytes = 0, query_bytes = 0;
};
static_assert(sizeof(ExtensionBatchHeader) == 96);

struct ExtensionBatch {
    ExtensionBatchHeader header;
    std::vector<SeqPair> pairs;
    std::vector<uint8_t> reference, query;
};

inline ExtensionBatch read_extension_batch(const std::string &path) {
    std::ifstream input(path, std::ios::binary);
    ExtensionBatch batch;
    input.read(reinterpret_cast<char *>(&batch.header), sizeof(batch.header));
    const auto &h = batch.header;
    if (!input || h.magic != 0x5747534542415431 || h.version != 1
        || h.pair_size != sizeof(SeqPair) || h.parameters.count < 1
        || h.parameters.count > 65536 || h.reference_bytes > 64 * 1024 * 1024
        || h.query_bytes > 64 * 1024 * 1024
        || (h.parameters.precision != 8 && h.parameters.precision != 16))
        throw std::runtime_error("Invalid batch header: " + path);
    batch.pairs.resize(static_cast<size_t>(h.parameters.count) + 64);
    batch.reference.resize(h.reference_bytes);
    batch.query.resize(h.query_bytes);
    input.read(reinterpret_cast<char *>(batch.pairs.data()),
        h.parameters.count * sizeof(SeqPair));
    input.read(reinterpret_cast<char *>(batch.reference.data()), h.reference_bytes);
    input.read(reinterpret_cast<char *>(batch.query.data()), h.query_bytes);
    if (!input || input.peek() != std::char_traits<char>::eof())
        throw std::runtime_error("Invalid batch payload: " + path);
    for (int i = 0; i < h.parameters.count; ++i) {
        const auto &pair = batch.pairs[i];
        if (pair.idr < 0 || pair.idq < 0 || pair.len1 < 0 || pair.len2 < 0
            || uint64_t(pair.idr) + pair.len1 > h.reference_bytes
            || uint64_t(pair.idq) + pair.len2 > h.query_bytes)
            throw std::runtime_error("Invalid batch offsets: " + path);
    }
    return batch;
}
