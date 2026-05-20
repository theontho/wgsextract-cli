HG37_BUILD_ALIASES = {"hg19", "hg37", "grch37", "hs37", "hs37d5"}
HG38_BUILD_ALIASES = {"hg38", "grch38", "hs38", "hs38dh"}
T2T_BUILD_ALIASES = {
    "t2t",
    "t2tv20",
    "t2tv2",
    "t2tv2.0",
    "chm13",
    "chm13v2",
    "chm13v2.0",
}

BUILD_CHOICES = [
    "hg38",
    "GRCh38",
    "hs38",
    "hs38DH",
    "hs38d1",
    "hs38d1v0",
    "hg19",
    "hg37",
    "GRCh37",
    "hs37",
    "hs37d5",
    "t2t",
    "T2Tv20",
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


def ploidy_for_build(build: str) -> str:
    if is_hg37_build(build):
        return "GRCh37"
    if is_hg38_build(build) or is_t2t_build(build):
        return "GRCh38"
    raise ValueError(f"Unsupported reference build for ploidy: {build}")


def build_from_path(path: str) -> str | None:
    path_lower = path.lower()
    if any(alias in path_lower for alias in ["hg38", "grch38", "hs38"]):
        return "hg38"
    if any(alias in path_lower for alias in ["hg19", "hg37", "grch37", "hs37"]):
        return "hg19"
    if any(alias in path_lower for alias in ["t2t", "chm13"]):
        return "t2t"
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
