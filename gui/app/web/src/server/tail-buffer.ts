/**
 * Keeps the last `limit` bytes written to it. Chunks are stored as-is and
 * trimmed from the head, so appends are amortized O(chunk) regardless of how
 * much output a process produces.
 */
export class TailBuffer {
    private chunks: Buffer[] = [];
    private head = 0;
    private size = 0;
    private dropped = false;

    constructor(private readonly limit: number) {}

    get truncated(): boolean {
        return this.dropped;
    }

    push(chunk: Buffer): void {
        if (chunk.length === 0) {
            return;
        }
        if (chunk.length >= this.limit) {
            this.chunks = this.limit > 0 ? [Buffer.from(chunk.subarray(chunk.length - this.limit))] : [];
            this.head = 0;
            this.dropped ||= chunk.length > this.limit || this.size > 0;
            this.size = Math.max(0, this.limit);
            return;
        }
        this.chunks.push(chunk);
        this.size += chunk.length;
        while (this.size > this.limit) {
            const first = this.chunks[this.head];
            if (!first) {
                break;
            }
            const excess = this.size - this.limit;
            if (first.length <= excess) {
                this.head += 1;
                this.size -= first.length;
            }
            else {
                this.chunks[this.head] = first.subarray(excess);
                this.size -= excess;
            }
            this.dropped = true;
        }
        if (this.head > 64 && this.head * 2 > this.chunks.length) {
            this.chunks = this.chunks.slice(this.head);
            this.head = 0;
        }
    }

    text(): string {
        const bytes = Buffer.concat(this.chunks.slice(this.head), this.size);
        // A truncated head may start inside a multi-byte UTF-8 sequence; skip its continuation bytes.
        let start = 0;
        while (this.dropped && start < Math.min(3, bytes.length) && ((bytes[start] ?? 0) & 0xc0) === 0x80) {
            start += 1;
        }
        return bytes.toString("utf8", start);
    }
}
