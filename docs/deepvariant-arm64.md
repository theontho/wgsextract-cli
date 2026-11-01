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

Initial public HG002 chr20 qualification has 210,390 identical sites and
genotypes against the pinned x64 control. Differences are confined to
QUAL (at most 0.5), GQ (at most 2) and PL (at most 1), not alleles, FILTER,
depth or model routing. FP32 TensorFlow kernels differ between architectures;
confidence fields are not promised byte-identical. Other models remain
unqualified until equivalent evidence exists.
