# Oxford Nanopore

`align` and `realign` detect ONT from `@RG PL:ONT` or UUID read names in
BAM/CRAM/FASTQ (including gzipped FASTQ). Read groups take precedence; mixed
platforms are not assigned one platform. An explicit `--platform` overrides
detection. Unknown input still defaults to Illumina. SRA-renamed FASTQs without
platform metadata need `--platform ont`.

CPU ONT alignment uses minimap2 `map-ont`, carries `MM`, `ML` and `MN` through
`samtools fastq -T MM,ML,MN` and `minimap2 -y`, and soft-clips supplementary
alignments (`-Y`) so the modification coordinates still describe the full read.
Sorting retains these tags.
FASTQ input first passes through `samtools import -T MM,ML,MN`, so ordinary
ONT `runid`/channel comments do not become invalid SAM fields with `-y`.
Realignment streams single-end tagged FASTQ instead
of reusing potentially untagged paired FASTQs. `--keep-fastq` and GPU ONT
alignment are refused rather than silently losing modification data; for a
retained intermediate use `samtools fastq -T MM,ML,MN` separately.

Platform detection and tagged CPU alignment/realignment are supported.
Whole-genome ONT accuracy, variant calling and methylation analysis are not
qualified by this change; whole-genome validation is tracked in #299.

## CLI / GUI contract

No new option is needed for detection. Leave `--platform` unset for automatic
selection; explicitly sending `--platform illumina` disables it. The CLI logs
the detected platform. `info` recognizes `PL:ONT` even when read names were
renamed. BAM/CRAM variant callers share the same cached, file-revision-aware
platform resolver; detection alone does not qualify short-read callers for ONT.
GUI integrations should allow automatic platform selection and surface the
tag-preservation limitations above.
