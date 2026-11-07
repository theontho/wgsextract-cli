/* wgse_ksw_align2_batch() (ksw_oracle.c.in) must return exactly what upstream
 * ksw_align2() returns for every task: random scoring, gap penalties, query and
 * window lengths (mixed and shared segment lengths), ambiguous bases, planted
 * matches, 8-bit saturation and 16-bit tasks, with and without start search. */
#include "../ksw_batch.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint64_t rng = 0x9E3779B97F4A7C15ULL;
static uint32_t rnd(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (uint32_t)rng; }

int main(int argc, char **argv)
{
	int rounds = argc > 1 ? atoi(argv[1]) : 3000, bad = 0, n_tasks = 0, n_rev = 0, n_sat = 0, n_i16 = 0;
	for (int round = 0; round < rounds; ++round) {
		int8_t mat[25];
		int a = 1 + rnd() % 4, b = rnd() % 8 ? 1 + rnd() % 20 : 60 + rnd() % 70, k = 0, i, j;
		int o_del = rnd() % 10, e_del = 1 + rnd() % 4, o_ins = rnd() % 10, e_ins = 1 + rnd() % 4;
		if (rnd() % 4 == 0) { a = 1; b = 4; o_del = o_ins = 6; e_del = e_ins = 1; }
		for (i = 0; i < 4; ++i) { for (j = 0; j < 4; ++j) mat[k++] = i == j ? a : -b; mat[k++] = -1; }
		for (j = 0; j < 5; ++j) mat[k++] = -1;
		int n = 1 + rnd() % 9, same_len = rnd() % 2, ql0 = 1 + rnd() % 300;
		wgse_ksw_task_t t[9];
		kswr_t want[9];
		uint8_t *ref = malloc(2000);
		for (i = 0; i < 2000; ++i) ref[i] = rnd() % 4;
		for (k = 0; k < n; ++k) {
			int ql = same_len ? ql0 : 1 + rnd() % 300, tl = 1 + rnd() % 1500;
			uint8_t *q = malloc(ql), *tg = malloc(tl);
			for (i = 0; i < tl; ++i) tg[i] = rnd() % 64 == 0 ? 4 : ref[(i + round) % 2000];
			int src = rnd() % 2000;
			int ambiguous = rnd() % 2, errors = rnd() % 2 ? 30 : 1 << 30;
			for (i = 0; i < ql; ++i) q[i] = ambiguous && rnd() % 32 == 0 ? 4 : ref[(src + i) % 2000];
			if (rnd() % 2) { // plant the query in the target
				int off = tl > ql ? rnd() % (tl - ql + 1) : 0;
				for (i = 0; i < ql && off + i < tl; ++i) tg[off + i] = rnd() % errors ? q[i] % 4 : rnd() % 4;
			}
			int minsc = rnd() % 4 ? 19 * a : rnd() % 40;
			int xtra = (ql * a < 250 && rnd() % 8 ? KSW_XBYTE : 0) | (rnd() % 8 ? KSW_XSUBO | KSW_XSTART : rnd() % 2 ? KSW_XSTART : KSW_XSTOP) | (minsc & 0xffff);
			t[k] = (wgse_ksw_task_t){ ql, tl, xtra, q, tg, { 0 } };
			want[k] = ksw_align2(ql, q, tl, tg, 5, mat, o_del, e_del, o_ins, e_ins, xtra, 0);
			n_i16 += !(xtra & KSW_XBYTE);
			n_sat += want[k].score == 255 && (xtra & KSW_XBYTE);
			n_rev += want[k].tb >= 0;
		}
		wgse_ksw_align2_batch(n, t, 5, mat, o_del, e_del, o_ins, e_ins);
		for (k = 0; k < n; ++k, ++n_tasks) {
			if (memcmp(&t[k].r, &want[k], sizeof(kswr_t)) && bad++ < 5)
				printf("mismatch round %d task %d ql=%d tl=%d xtra=%#x: %d,%d,%d,%d,%d,%d,%d vs %d,%d,%d,%d,%d,%d,%d\n", round, k, t[k].qlen, t[k].tlen, t[k].xtra,
					t[k].r.score, t[k].r.te, t[k].r.qe, t[k].r.score2, t[k].r.te2, t[k].r.tb, t[k].r.qb,
					want[k].score, want[k].te, want[k].qe, want[k].score2, want[k].te2, want[k].tb, want[k].qb);
			free(t[k].query); free(t[k].target);
		}
		free(ref);
	}
	printf("tasks=%d with_start=%d saturated_u8=%d i16=%d mismatches=%d\n", n_tasks, n_rev, n_sat, n_i16, bad);
	return bad || !n_rev || !n_sat || !n_i16;
}
