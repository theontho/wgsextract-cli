HG37_BUILD_ALIASES = {"hg19", "hg37", "grch37", "hs37", "hs37d5"}
HG38_BUILD_ALIASES = {"hg38", "grch38", "hs38", "hs38dh", "hs38d1", "hs38d1v0"}
T2T_BUILD_ALIASES = {
    "t2t",
    "t2tv20",
    "t2tv2",
    "t2tv2.0",
    "chm13",
    "chm13v2",
    "chm13v2.0",
}

# Filename substrings that identify each canonical build, in preference order.
BUILD_PATH_TOKENS = {
    "hg38": ("hg38", "grch38", "hs38"),
    "hg19": ("hg19", "hg37", "grch37", "hs37"),
    "t2t": ("t2t", "chm13"),
}

# chr1 lengths are distinct per assembly and survive any contig naming scheme.
CHR1_LENGTH_BUILDS = {
    249250621: "hg19",
    248956422: "hg38",
    248387328: "t2t",
}

BUILD_CHOICES = [
    *sorted(HG38_BUILD_ALIASES),
    "GRCh38",
    "hs38DH",
    *sorted(HG37_BUILD_ALIASES),
    "GRCh37",
    *sorted(T2T_BUILD_ALIASES),
    "T2Tv20",
    "T2Tv2",
    "T2Tv2.0",
    "CHM13",
    "CHM13v2.0",
]


def _build_key(build: str) -> str:
    return build.strip().lower()


def is_hg37_build(build: str) -> bool:
    return _build_key(build) in HG37_BUILD_ALIASES


def is_hg38_build(build: str) -> bool:
    key = _build_key(build)
    return key in HG38_BUILD_ALIASES or key.startswith("hs38d1")


def is_t2t_build(build: str) -> bool:
    return _build_key(build) in T2T_BUILD_ALIASES


def canonical_build(build: str | None) -> str | None:
    """Map any recognized build alias to "hg19", "hg38" or "t2t"."""
    if not build:
        return None
    if is_hg37_build(build):
        return "hg19"
    if is_hg38_build(build):
        return "hg38"
    if is_t2t_build(build):
        return "t2t"
    return None


def ploidy_for_build(build: str) -> str:
    if is_hg37_build(build):
        return "GRCh37"
    if is_hg38_build(build) or is_t2t_build(build):
        return "GRCh38"
    raise ValueError(f"Unsupported reference build for ploidy: {build}")


def build_from_path(path: str) -> str | None:
    path_lower = path.lower()
    for build, tokens in BUILD_PATH_TOKENS.items():
        if any(token in path_lower for token in tokens):
            return build
    return None


def build_from_chr1_length(contig_lengths: dict[str, int]) -> str | None:
    """Identify the human assembly from the chr1 length of a sequence dictionary."""
    for name in ("chr1", "1"):
        if name in contig_lengths:
            return CHR1_LENGTH_BUILDS.get(contig_lengths[name])
    return None


def fake_data_library_code(build: str) -> str | None:
    key = _build_key(build)
    if key == "hg19":
        return "hg19"
    if is_hg37_build(build):
        return "hs37d5"
    if is_hg38_build(build):
        return "hg38"
    if is_t2t_build(build):
        return "T2Tv20"
    return None
