# Native Windows tools

Bioconda builds its tools for Linux and macOS only. On Windows, Pixi installs
WGS Extract's own builds of them instead, made from these recipes:

| Package | Executables |
| --- | --- |
| htslib | bgzip, tabix, htsfile |
| samtools | samtools |
| bcftools | bcftools (no plugins) |
| bwa | bwa |
| bwa-mem2 | bwa-mem2 (picks the build for the CPU: SSE4.1 to AVX-512) |
| minimap2 | minimap2 |
| fastp | fastp |
| samblaster | samblaster |

Each is a native Windows executable built with MSYS2's UCRT64 GCC and linked
statically, so it needs only Windows' own DLLs: no MSYS2, Cygwin or WSL on the
user's PC, and no Linux VM between the tools and the files. Each is a conda
package (`Library\bin\*.exe`) that `pixi.toml` pins by URL and SHA-256.

## Porting

Unix tools assume what Windows doesn't give them. The recipes deal with:

- **32-bit `long`.** Windows is LLP64: `long` is 32 bits, so file offsets
  (`ftell`, `fseek`), counters and bit masks that use it overflow. A human BWA
  index (`.bwt`, 3 GB) made `bwa mem` fail at once with `[ftell] Invalid
  argument`, bwa-mem2's 32-bit popcount and mask bits kept it seeding forever,
  and fastp's read and base counts overflow on a 30x genome. Patches widen them.
- **Binary I/O.** Windows' C runtime rewrites `\n` as `\r\n` in text mode, and
  UCRT's `binmode.o` is empty. `compat/wgse_compat.c` makes all I/O binary
  before `main()`, so output matches Linux byte for byte.
- **Missing POSIX functions.** `compat/` supplies the drand48 family (POSIX's
  exact generator, so BWA's random choices match Linux), `getrusage`, `times`,
  anonymous `mmap`, `pread`/`pwrite` and `getline`. The header is
  force-included (`-include wgse_compat.h`) and the object linked into each tool.
- **No `fork`/`exec`.** bwa and bwa-mem2 lose their `< command` input pipes and
  `bwa shm`; bwa-mem2's launcher starts its CPU-specific build as a child
  process and waits for it, since Windows' `execv` returns immediately.

Each tool's output was compared with the Linux Bioconda build on the same
input, using real reads against the full hs38DH reference: bwa, bwa-mem2
(including its index), minimap2, samblaster, fastp, and the `align`, `vcf snp`,
`bam unalign` and `microarray` workflows give identical results.

## Building

The [Windows Tools](../../.github/workflows/windows_tools.yml) workflow builds
every recipe on pull requests that change them. To release a set, run it with
a new `release_tag` (`windows-tools-<n>`), then pin it:

```sh
python packaging/windows/pin.py windows-tools-<n>
pixi lock
```

To build on a Windows PC, install [MSYS2](https://www.msys2.org) and the
packages in [msys2-packages.txt](msys2-packages.txt) (from an MSYS2 shell:
`grep -v '^#' msys2-packages.txt | xargs pacman -S --needed`), then from the
repository root:

```powershell
$env:MSYS2_ROOT = "C:\msys64"   # where MSYS2 is
pixi exec --spec rattler-build==0.77.0 rattler-build build `
    --recipe-dir packaging/windows/recipes --output-dir out/conda --target-platform win-64
```

A recipe is `recipe.yaml` (source URL and SHA-256, patches, tests) plus
`build.sh`, which sources [recipes/env.sh](recipes/env.sh) for the shared
flags. To change a patch, unpack the source into a git repository, apply the
patch, edit, and regenerate it with `git diff` (some upstream files are CRLF;
`.gitattributes` keeps patches byte-exact).
