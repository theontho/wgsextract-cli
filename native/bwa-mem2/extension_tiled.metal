struct ExtensionState {
    int best, best_i, best_j, global_score, global_end, max_offset;
    int begin, end, cells;
    bool done;
};

int extension_width(Pair pair, constant Parameters &p) {
    int width = min(pair.bandwidth, max(1,
        (pair.len2 * p.max_substitution + p.end_bonus - p.open_ins) / p.extend_ins + 1));
    return min(width, max(1,
        (pair.len2 * p.max_substitution + p.end_bonus - p.open_del) / p.extend_del + 1));
}

void finish_row(thread ExtensionState &s, Pair pair, constant Parameters &p,
    int i, int begin, int end, int h, int best, int position, int first, int last)
{
    s.cells += max(0, end - begin);
    if (max(begin, end) == pair.len2 && h >= s.global_score) {
        s.global_score = h;
        s.global_end = i;
    }
    if (best == 0) s.done = true;
    else if (best > s.best) {
        s.best = best;
        s.best_i = i;
        s.best_j = position;
        s.max_offset = max(s.max_offset, abs(position - i));
    } else if (p.zdrop > 0) {
        int drift = (i - s.best_i) - (position - s.best_j);
        int penalty = drift > 0 ? drift * p.extend_del : -drift * p.extend_ins;
        if (s.best - best - penalty > p.zdrop) s.done = true;
    }
    s.begin = first;
    s.end = min(pair.len2, max(last, first - 1) + 2);
}

template<typename Row>
void serial_row(Row boundary, const device uint *query,
    Pair pair, constant Parameters &p, thread ExtensionState &s, int i, int base, int width)
{
    int begin = max(s.begin, i - width);
    int end = min(s.end, min(pair.len2, i + width + 1));
    if (p.precision != 0 && begin >= end) {
        s.done = true;
        return;
    }
    int h = begin == 0 ? max(0, pair.h0 - p.open_del - p.extend_del * (i + 1)) : 0;
    int f = 0, row_best = 0, row_j = -1, first = end, last = begin - 1;
    for (int tile = (begin / 8) * 8; tile < end; tile += 8) {
        uint word = query[pair.idq + (tile / 8) * 32];
        ushort2 state[8];
        #pragma unroll
        for (int k = 0; k < 8; ++k) state[k] = boundary[(tile + k) * 32];
        #pragma unroll
        for (int k = 0; k < 8; ++k) {
            int j = tile + k;
            if (j < begin || j >= end) continue;
            int diagonal = int(state[k].x), e = int(state[k].y);
            int q = int((word >> (k * 4)) & 15);
            int substitution = p.simple_matrix
                ? (base == 4 || q == 4 ? int(p.matrix[24])
                    : base == q ? int(p.matrix[0]) : int(p.matrix[1]))
                : int(p.matrix[base * 5 + q]);
            int match = diagonal == 0 ? 0 : diagonal + substitution;
            state[k].x = ushort(h);
            h = max(match, max(e, f));
            e = max(e - p.extend_del, max(0, match - p.open_del - p.extend_del));
            f = max(f - p.extend_ins, max(0, match - p.open_ins - p.extend_ins));
            state[k].y = ushort(e);
            if (state[k].x != 0 || e != 0) {
                first = min(first, j);
                last = j;
            }
            if (h >= row_best) {
                row_best = h;
                row_j = j;
            }
        }
        #pragma unroll
        for (int k = 0; k < 8; ++k) boundary[(tile + k) * 32] = state[k];
    }
    boundary[end * 32] = ushort2(h, 0);
    if (h != 0) last = end;
    finish_row(s, pair, p, i, begin, end, h, row_best, row_j, first, last);
}

void write_extension(device Pair &out, ExtensionState s, int issued) {
    out.score = s.best;
    out.qle = s.best_j + 1;
    out.tle = s.best_i + 1;
    out.gtle = s.global_end + 1;
    out.gscore = s.global_score;
    out.max_off = s.max_offset;
    out.regid = s.cells;
    out.idr = issued;
}

kernel void extend_striped(
    device Pair *pairs [[buffer(0)]], const device uint *reference [[buffer(1)]],
    const device uint *query [[buffer(2)]], constant Parameters &p [[buffer(3)]],
    device ushort2 *scratch [[buffer(4)]], uint index [[thread_position_in_grid]])
{
    if (index >= uint(p.count)) return;
    Pair pair = pairs[index];
    device ushort2 *boundary = scratch + pair.id;
    for (int j = 0; j <= ((pair.len2 + 7) / 8) * 8; ++j)
        boundary[j * 32] = ushort2(j == 0 ? pair.h0
            : max(0, pair.h0 - p.open_ins - p.extend_ins * j), 0);
    ExtensionState s{pair.h0, -1, -1, -1, -1, 0, 0, pair.len2, 0, pair.len2 == 0};
    int width = extension_width(pair, p);
    for (int block = 0; block < pair.len1 && !s.done; block += 8) {
        uint word = reference[pair.idr + (block / 8) * 32];
        for (int r = 0; r < 8 && block + r < pair.len1 && !s.done; ++r)
            serial_row(boundary, query, pair, p, s, block + r,
                int((word >> (r * 4)) & 15), width);
    }
    write_extension(pairs[index], s, s.cells);
}

kernel void extend_shared(
    device Pair *pairs [[buffer(0)]], const device uint *reference [[buffer(1)]],
    const device uint *query [[buffer(2)]], constant Parameters &p [[buffer(3)]],
    uint index [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]])
{
    if (index >= uint(p.count)) return;
    Pair pair = pairs[index];
    threadgroup ushort2 rows[129 * 32];
    threadgroup ushort2 *boundary = rows + lane;
    for (int j = 0; j <= ((pair.len2 + 7) / 8) * 8; ++j)
        boundary[j * 32] = ushort2(j == 0 ? pair.h0
            : max(0, pair.h0 - p.open_ins - p.extend_ins * j), 0);
    ExtensionState s{pair.h0, -1, -1, -1, -1, 0, 0, pair.len2, 0, pair.len2 == 0};
    int width = extension_width(pair, p);
    for (int block = 0; block < pair.len1 && !s.done; block += 8) {
        uint word = reference[pair.idr + (block / 8) * 32];
        for (int r = 0; r < 8 && block + r < pair.len1 && !s.done; ++r)
            serial_row(boundary, query, pair, p, s, block + r,
                int((word >> (r * 4)) & 15), width);
    }
    write_extension(pairs[index], s, s.cells);
}

// Real 8x8 register reuse. Row trimming is a full-row dependency: speculate
// bounded row ends, validate every row decision, and roll back just this block
// if the exact band differs. No approximation reaches the returned tuple.
kernel void extend_tiled(
    device Pair *pairs [[buffer(0)]], const device uint *reference [[buffer(1)]],
    const device uint *query [[buffer(2)]], constant Parameters &p [[buffer(3)]],
    device ushort2 *scratch [[buffer(4)]], uint index [[thread_position_in_grid]])
{
    if (index >= uint(p.count)) return;
    Pair pair = pairs[index];
    device ushort2 *boundary = scratch + pair.id;
    device ushort2 *backup = boundary + pair.regid;
    int rounded = ((pair.len2 + 7) / 8) * 8;
    for (int j = 0; j <= rounded; ++j)
        boundary[j * 32] = ushort2(j == 0 ? pair.h0
            : max(0, pair.h0 - p.open_ins - p.extend_ins * j), 0);
    ExtensionState s{pair.h0, -1, -1, -1, -1, 0, 0, pair.len2, 0, pair.len2 == 0};
    int width = extension_width(pair, p), issued = 0;
    for (int block = 0; block < pair.len1 && !s.done; block += 8) {
        uint target_word = reference[pair.idr + (block / 8) * 32];
        int rows = min(8, pair.len1 - block);
        ExtensionState saved = s;
        for (int j = 0; j <= rounded; ++j) backup[j * 32] = boundary[j * 32];
        short2 bounds[8];
        short4 activity[8];
        int packed_best[8];
        ushort2 carry[8];
        #pragma unroll
        for (int r = 0; r < 8; ++r) {
            bounds[r] = short2(max(s.begin, block + r - width),
                min(pair.len2, min(s.end + 2 * r, block + r + width + 1)));
            activity[r] = short4(pair.len2 + 1, -1, pair.len2 + 1, -1);
            packed_best[r] = 0;
            carry[r] = ushort2(bounds[r].x == 0
                ? max(0, pair.h0 - p.open_del - p.extend_del * (block + r + 1)) : 0, 0);
            if (r < rows) issued += max(0, int(bounds[r].y) - int(bounds[r].x));
        }
        int tile_end = min(pair.len2,
            min(s.end + 2 * (rows - 1), block + rows - 1 + width + 1));
        for (int tile = (int(bounds[0].x) / 8) * 8; tile < tile_end; tile += 8) {
            uint query_word = query[pair.idq + (tile / 8) * 32];
            ushort2 state[8];
            #pragma unroll
            for (int k = 0; k < 8; ++k) state[k] = boundary[(tile + k) * 32];
            #pragma unroll
            for (int r = 0; r < 8; ++r) {
                if (r >= rows) continue;
                int h = int(carry[r].x), f = int(carry[r].y);
                int base = int((target_word >> (r * 4)) & 15);
                #pragma unroll
                for (int k = 0; k < 8; ++k) {
                    int j = tile + k;
                    if (j < int(bounds[r].x) || j >= int(bounds[r].y)) continue;
                    int diagonal = int(state[k].x), e = int(state[k].y);
                    if (diagonal != 0 || e != 0) activity[r].w = short(j);
                    int q = int((query_word >> (k * 4)) & 15);
                    int substitution = p.simple_matrix
                        ? (base == 4 || q == 4 ? int(p.matrix[24])
                            : base == q ? int(p.matrix[0]) : int(p.matrix[1]))
                        : int(p.matrix[base * 5 + q]);
                    int match = diagonal == 0 ? 0 : diagonal + substitution;
                    state[k].x = ushort(h);
                    h = max(match, max(e, f));
                    e = max(e - p.extend_del, max(0, match - p.open_del - p.extend_del));
                    f = max(f - p.extend_ins, max(0, match - p.open_ins - p.extend_ins));
                    state[k].y = ushort(e);
                    if (h != 0) {
                        activity[r].x = min(activity[r].x, short(j));
                        activity[r].y = short(j);
                    }
                    if (e != 0) activity[r].z = min(activity[r].z, short(j));
                    packed_best[r] = max(packed_best[r], h * 512 + j + 1);
                }
                #pragma unroll
                for (int k = 0; k < 8; ++k)
                    if (tile + k == int(bounds[r].y)) state[k] = ushort2(h, 0);
                carry[r] = ushort2(h, f);
            }
            #pragma unroll
            for (int k = 0; k < 8; ++k) boundary[(tile + k) * 32] = state[k];
        }
        #pragma unroll
        for (int r = 0; r < 8; ++r)
            if (r == rows - 1) boundary[int(bounds[r].y) * 32] = ushort2(carry[r].x, 0);
        bool retry = false;
        #pragma unroll
        for (int r = 0; r < 8; ++r) {
            if (r >= rows || s.done) continue;
            int begin = max(s.begin, block + r - width);
            int end = min(s.end, min(pair.len2, block + r + width + 1));
            if (p.precision != 0 && begin >= end) {
                s.done = true;
                break;
            }
            // A wider speculative halo is harmless only if it produces no
            // nonzero cells and overwrites no stale state beyond the endpoint.
            if (begin < int(bounds[r].x) || end > int(bounds[r].y) || int(activity[r].x) < begin
                || int(activity[r].y) >= end || int(activity[r].w) > end) {
                retry = true;
                break;
            }
            int h = int(carry[r].x);
            int first = min(end, min(int(activity[r].x) + 1, int(activity[r].z)));
            if (begin == 0 && pair.h0 > p.open_del + p.extend_del * (block + r + 1))
                first = 0;
            int last = max(begin - 1, int(activity[r].y) + 1);
            finish_row(s, pair, p, block + r, begin, end, h,
                packed_best[r] / 512, packed_best[r] % 512 - 1, first, last);
        }
        if (retry) {
            s = saved;
            for (int j = 0; j <= rounded; ++j) boundary[j * 32] = backup[j * 32];
            for (int r = 0; r < rows && !s.done; ++r)
                serial_row(boundary, query, pair, p, s, block + r,
                    int((target_word >> (r * 4)) & 15), width);
            issued += s.cells - saved.cells;
        }
    }
    write_extension(pairs[index], s, issued);
}
