# Native Linux ARM64 DeepVariant

The CPU path uses a locally built **DeepVariant 1.10.0** image on Linux aarch64.
Google's released image is x86-only; this is an unofficial native source build,
not an official Google ARM distribution. No QEMU, x86 image, ONNX conversion,
INT8 quantization or BF16 autoconfiguration is used.

## Provision dependencies explicitly

Install native Docker, Git and Python 3 before building. The CLI never installs
dependencies or pulls/builds an image when calling variants. Building is a
separate, explicit administrator/developer action; use a native ARM64 Linux
builder with 64 GB RAM and enough disk space for Bazel/TensorFlow.

From a checkout of this repository on that builder:

```bash
bash packaging/deepvariant-arm64/build.sh out/deepvariant-arm64-build
```

This creates `wgsextract/deepvariant:1.10.0-arm64`, the Linux ARM64 default.
There is no published image assumed at this tag. For another installed tag or
immutable image digest, set the existing configuration option:

```toml
deepvariant_image = "my-local-native-image@sha256:..."
```

Calling uses the normal command and flags, for example:

```bash
wgsextract vcf deepvariant -i sample.bam --ref reference.fna -r chr20 -t 16 -o results
```

Both Docker's engine architecture and the image's `linux/arm64` metadata are
validated before the caller prepares inputs. An arbitrary PATH launcher is not
used on ARM: its Python, native modules and child executables cannot be proved
native from its shell-script architecture. The container entrypoint is explicitly
overridden to `run_deepvariant`, preventing community auto-configuration scripts
from silently switching inference precision. Writable home/temporary files stay
inside the caller's intermediate directory and are removed after the run.

The experimental ULTIMA image and Parabricks `--gpu` remain unavailable on
Linux ARM64. WGS is the qualification target; other upstream release models are
packaged but require their own real-data qualification.

## Source and numerical provenance

- Official scientific source: Google `v1.10.0`,
  `d6b15e21323d9d15b347083f1d256ad62998c4cf`.
- ARM build scaffolding: Lambda-Biolab `v1.10.0-arm64.1`,
  `ff2a2a949c8dd244908499ceb6a9fda381fcccf6`, BSD-3.
- Only the fork's HTS NEON selection and libssw sse2neon build declarations are
  retained in scientific directories. Its CIGAR normalization limit, haplotype
  cap, image-buffer changes and modified inference code are **not** retained.
- TensorFlow CPU 2.16.1 and the original 1.10.0 SavedModels/small models are used.
  Essential Python runtime versions match the pinned x64 Google image, including
  NumPy, JAX, Keras and ml-dtypes. Fixed internal TensorFlow image settings are
  not additional user/environment-variable configuration.
- `out/deepvariant-arm64-build/scientific-source.patch` records the actual source
  delta. The image includes the upstream sources/licenses and model SHA256s.
  All 38 model blobs are checksum-verified against the immutable x64 image
  `google/deepvariant:1.10.0@sha256:962e5a83b1d76aae6990625d47102785f791603f2138aa1fa9aa4fb6a2eecbe6`.
  The relocated Python 3.10 virtualenv and Bazel launchers support the invoking
  user's non-root UID; system Python is not used to import its native modules.

Image metadata alone cannot prove every binary native. Audit the interpreter,
TensorFlow libraries, and the ELF modules inside all three Bazel ZIPs using
`file`/ELF `e_machine=183`, and compare model hashes against the x64 golden.
Do not substitute the community image on the strength of its F1 claims: its
scientific changes are not equivalent to this source build.

x64/Linux and macOS defaults and commands are unchanged. Qualification evidence,
unnormalized VCF differences, matched-vCPU timings and cloud cost are recorded
in the task's `out/arm64/deepvariant/report.md`; a draft PR does not by itself
establish full-genome or performance qualification.

Public HG002 chr20 qualification has 210,390 identical sites and
genotypes against the pinned x64 control. Differences are confined to
QUAL (at most 0.5), GQ (at most 2) and PL (at most 1), not alleles, FILTER,
depth or model routing. FP32 TensorFlow kernels differ between architectures;
confidence fields are not promised byte-identical. Other models remain
unqualified until equivalent evidence exists.

All 81,898 large-model input tensors and decoded candidate variants match
exactly. Maximum probability differences before postprocessing are 2.81e-6
(large model) and 2.39e-7 (small model), with no argmax changes. Three repetitions
on each architecture have identical within-architecture VCF bodies.

Matched standard 16-vCPU/64-GB Azure guests give a three-run chr20 wall median
of 487.97 s on Cobalt AArch64 versus 424.51 s on AMD x64: **14.95% slower**,
within the 20% target. ARM has 16 physical cores; x64 has eight with SMT.
CPU-time medians are 6,614.15 / 6,173.57 s and maximum-child RSS medians
26,979,616 / 12,075,748 KiB (ARM / x64). The native path needs materially
more memory.

## Completed public whole-genome qualification

The complete public HG002 35x BAM (46,033,519,192 bytes) was verified on both
guests: SHA256 `b5f1255aa08ecda8f0ec01682d32d9a37a7327ca21ea681d94c250f6a2f22de6`.
Both original WGS pipelines completed successfully, with no region restriction.
All **7,709,239 REF/ALT sites** match; all PASS-site genotypes match.
One non-PASS genotype differs: chr1:31445438 CG>C is x64 `0/0;RefCall;GQ=20`
versus ARM `./.;NoCall;GQ=19`. Its unchanged reference argmax has actual Phred
scores 19.5000106 / 19.4999183. Original integer rounding and the GQ-20
hom-reference no-call safeguard explain this difference.

Confidence is not byte-identical. There are 244,796 changed records:
241,646 QUAL, 10,521 GQ, 351 PL, five MID changes and the above GT/FILTER change.
Maximum QUAL/GQ/PL deltas are 29.7 / 30 / 19, not the smaller chr20 bounds.
Two additional numerical effects explain those larger bounds:

- Six candidates cross the original small-model acceptance thresholds
  (SNP GQ 20 / indel GQ 28), changing their small/large-model route. Passing-side
  scores are only 20.0000300 or 28.0000156. Five final MID labels change; the
  sixth is a multiallelic merge at chr10:24436757 (PL delta 19).
  All six candidates retain the same argmax across the alternative routes.
- At chr6:170418998 C>T, the small model's homozygous-alt probability is
  exactly 1.0 on x64 versus 0.9999998807907104 on ARM. The original capped
  Phred calculation gives QUAL/GQ 99/99 versus 69.3/69 without changing GT or PL.

Every actual full-run CVO was compared: all 8,286,723 combined candidate keys
match, including the six cross-stage substitutions. The 8,286,717 same-stage
vectors have no argmax differences; maximum absolute errors are 3.10e-6
(large model) and 4.77e-7 (small model). The actual probabilities, original
threshold/rounding source and exhaustive unnormalized VCF differences are saved.
Full-genome image tensors were not traced; the tensor-identity claim is chr20-only.
These boundaries are disclosed, not hidden by precision changes or VCF rewriting.

| Whole-genome measurement | x64 | AArch64 |
|---|---:|---:|
| Wall seconds | 13,143 (3:39:03) | 14,157 (3:55:57) |
| User + system CPU seconds | 202,727.02 | 208,481.02 |
| Maximum-child RSS KiB | 12,381,064 | 25,920,200 |
| Aggregate cgroup peak bytes, including cache | 64,424,509,440 | 54,063,599,616 |

The matched-vCPU whole-genome ARM wall time is **7.72% slower**, meeting the
20% target. This is one complete run per architecture, separately from the
three-run chr20 medians. Both task-owned guests and related resources were
deleted after the outputs and scientific evidence were saved.
