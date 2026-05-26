import argparse
import hashlib
import json
import logging
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from wgsextract_cli.core.dev_download_cache import (
    benchmark_dataset_cache_root,
    dev_download_cache_enabled,
    drop_cached_download,
    mark_cache_item_used,
    prune_expired_cache_items,
    restore_cached_download,
    store_download_in_dev_cache,
)
from wgsextract_cli.core.download_progress import (
    copy_response_to_file,
    require_http_url,
)
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)

from ._benchmark_heavy import (
    _verified_checksum_path,
)
from ._benchmark_models import (
    BenchmarkDataset,
    BenchmarkDatasetSpec,
    BenchmarkDerivedAlignment,
    BenchmarkRemoteFile,
)


def _download_file(
    url: str, destination: Path, *, checksum_hint: str | None = None
) -> None:
    require_http_url(url, "benchmark dataset URL")
    tmp_path = destination.with_suffix(destination.suffix + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "wgsextract-cli"})
    logging.info("Downloading benchmark dataset to %s", destination.name)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            with open(tmp_path, "wb") as handle:
                copy_response_to_file(
                    response,
                    handle,
                    progress_label=destination.name,
                )
        tmp_path.replace(destination)
    except (OSError, urllib.error.URLError, WGSExtractError) as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise WGSExtractError(
            f"Failed to download benchmark dataset {destination.name}: {exc}"
        ) from exc


def _md5(path: Path) -> str:
    try:
        digest = hashlib.md5(usedforsecurity=False)
    except TypeError:
        digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_md5(path: Path, expected: str) -> None:
    normalized = expected.lower().strip()
    if not normalized:
        return
    actual = _md5(path)
    if actual != normalized:
        raise WGSExtractError(
            f"MD5 mismatch for {path}: expected {normalized}, got {actual}"
        )


def _write_verified_md5_marker(path: Path, marker_path: Path, md5: str) -> None:
    stat = path.stat()
    marker_path.write_text(
        "\n".join(
            [
                md5.lower().strip(),
                f"size={stat.st_size}",
                f"mtime_ns={stat.st_mtime_ns}",
            ]
        )
        + "\n",
        encoding="ascii",
    )


def _has_current_verified_md5_marker(path: Path, marker_path: Path, md5: str) -> bool:
    if not marker_path.exists():
        return False
    try:
        lines = marker_path.read_text(encoding="ascii").splitlines()
        values = {
            key: value
            for line in lines
            if "=" in line
            for key, value in [line.split("=", 1)]
        }
        stat = path.stat()
    except OSError:
        return False
    return (
        lines[:1] == [md5.lower().strip()]
        and values.get("size") == str(stat.st_size)
        and values.get("mtime_ns") == str(stat.st_mtime_ns)
    )


def _cached_remote_dataset_file(remote: BenchmarkRemoteFile, cache_root: Path) -> Path:
    path = cache_root / remote.filename
    checksum_hint = f"md5:{remote.md5.lower().strip()}" if remote.md5 else None
    verified_path = _verified_checksum_path(path, remote.md5)
    if path.exists() and remote.md5 is None:
        store_download_in_dev_cache(remote.url, path, checksum_hint=checksum_hint)
        return path
    if (
        path.exists()
        and remote.md5
        and _has_current_verified_md5_marker(path, verified_path, remote.md5)
    ):
        store_download_in_dev_cache(remote.url, path, checksum_hint=checksum_hint)
        return path
    if path.exists() and remote.md5:
        _verify_md5(path, remote.md5)
        _write_verified_md5_marker(path, verified_path, remote.md5)
        store_download_in_dev_cache(remote.url, path, checksum_hint=checksum_hint)
        return path
    if not path.exists():
        if restore_cached_download(remote.url, path, checksum_hint=checksum_hint):
            try:
                if remote.md5:
                    _verify_md5(path, remote.md5)
                    _write_verified_md5_marker(path, verified_path, remote.md5)
                return path
            except WGSExtractError:
                drop_cached_download(remote.url, path, checksum_hint=checksum_hint)
                path.unlink(missing_ok=True)
        _download_file(remote.url, path, checksum_hint=checksum_hint)
    if remote.md5:
        _verify_md5(path, remote.md5)
        _write_verified_md5_marker(path, verified_path, remote.md5)
    store_download_in_dev_cache(remote.url, path, checksum_hint=checksum_hint)
    return path


def _prepare_derived_alignment(
    alignment: BenchmarkDerivedAlignment,
    cached_files: dict[str, Path],
    cache_root: Path,
) -> None:
    source = cached_files.get(alignment.source_role)
    ref = cached_files.get("ref")
    if source is None or ref is None:
        raise WGSExtractError(
            "Benchmark dataset derived alignment requires source alignment and ref."
        )

    output = cache_root / alignment.output_filename
    index = cache_root / alignment.index_filename
    if output.exists() and index.exists():
        cached_files["bam"] = output
        cached_files["bam_index"] = index
        return

    if output.exists():
        output.unlink()
    if index.exists():
        index.unlink()

    run_command(
        [
            "samtools",
            "view",
            "-C",
            "-T",
            str(ref),
            "-s",
            alignment.subsample,
            "-o",
            str(output),
            str(source),
        ]
    )
    run_command(["samtools", "index", str(output)])
    produced_index = Path(str(output) + ".crai")
    if produced_index != index and produced_index.exists():
        produced_index.replace(index)
    if not index.exists():
        raise WGSExtractError(f"Failed to create benchmark alignment index: {index}")

    cached_files["bam"] = output
    cached_files["bam_index"] = index


def _write_direct_dataset_manifest(
    spec: BenchmarkDatasetSpec, cache_root: Path, cached_files: dict[str, Path]
) -> None:
    metadata = spec.metadata or {}
    files = {
        role: path.name
        for role, path in sorted(cached_files.items())
        if role
        in {
            "ref",
            "ref_fai",
            "ref_gzi",
            "bam",
            "bam_index",
            "cram",
            "cram_index",
            "fastq_r1",
            "fastq_r2",
            "vcf",
            "vcf_index",
            "targets",
            "targets_index",
            "source_cram",
            "source_cram_index",
        }
    }
    manifest = {
        "dataset_id": spec.dataset_id,
        "tag": spec.tag,
        "description": spec.description,
        "sample": spec.sample,
        "build": spec.build,
        "kind": spec.kind,
        "default_region": spec.default_region,
        "region_safe": spec.region_safe,
        "source_files": [asdict(remote) for remote in spec.remote_files],
        "files": files,
    }
    manifest.update(metadata)
    (cache_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def _link_cached_dataset_manifest(cache_root: Path, marker_path: Path) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(cache_root) + "\n", encoding="utf-8")


def _real_dataset_cache_dir(args: argparse.Namespace, outdir: Path) -> Path:
    cache_dir = getattr(args, "dataset_cache_dir", None)
    if cache_dir:
        return Path(str(cache_dir)).expanduser().resolve()
    if dev_download_cache_enabled():
        root = benchmark_dataset_cache_root()
        prune_expired_cache_items(root)
        return root
    return outdir / "datasets"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _optional_dataset_file(root: Path, files: dict[str, Any], role: str) -> Path | None:
    value = files.get(role)
    if not isinstance(value, str) or not value:
        return None
    path = (root / value).resolve()
    if not _is_relative_to(path, root.resolve()):
        raise WGSExtractError(
            f"Benchmark dataset file role escapes dataset root: {role}"
        )
    return path


def _required_dataset_file(root: Path, files: dict[str, Any], role: str) -> Path:
    path = _optional_dataset_file(root, files, role)
    if path is None:
        raise WGSExtractError(
            f"Benchmark dataset is missing required file role: {role}"
        )
    return path


def _existing_dataset_outputs(dataset: BenchmarkDataset) -> list[Path]:
    outputs = [dataset.ref, dataset.bam]
    outputs.extend(
        path
        for path in (
            dataset.bam_index,
            dataset.cram,
            dataset.cram_index,
            dataset.fastq_r1,
            dataset.fastq_r2,
            dataset.vcf,
            dataset.vcf_index,
            dataset.targets,
            dataset.targets_index,
        )
        if path is not None
    )
    return outputs


def _validate_real_benchmark_dataset(dataset: BenchmarkDataset) -> None:
    missing = [
        str(path)
        for path in _existing_dataset_outputs(dataset)
        if not path.exists() or not path.is_file()
    ]
    if missing:
        raise WGSExtractError(
            "Benchmark dataset is incomplete. Missing file(s): " + ", ".join(missing)
        )
    if dataset.fastq_r1 and not dataset.fastq_r2:
        raise WGSExtractError("Benchmark dataset provides fastq_r1 without fastq_r2.")


def _load_real_benchmark_dataset(root: Path) -> BenchmarkDataset:
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        candidates = list(root.glob("*/manifest.json"))
        if len(candidates) == 1:
            root = candidates[0].parent
            manifest_path = candidates[0]
    if not manifest_path.exists():
        raise WGSExtractError(f"Benchmark dataset manifest not found under {root}")

    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise WGSExtractError(
            f"Benchmark dataset manifest has no files object: {manifest_path}"
        )

    dataset = BenchmarkDataset(
        dataset_id=str(manifest.get("dataset_id") or root.name),
        description=str(
            manifest.get("description") or "release-backed real benchmark dataset"
        ),
        build=str(manifest.get("build") or "hg19"),
        root=root,
        ref=_required_dataset_file(root, files, "ref"),
        bam=_required_dataset_file(root, files, "bam"),
        bam_index=_optional_dataset_file(root, files, "bam_index"),
        cram=_optional_dataset_file(root, files, "cram"),
        cram_index=_optional_dataset_file(root, files, "cram_index"),
        fastq_r1=_optional_dataset_file(root, files, "fastq_r1"),
        fastq_r2=_optional_dataset_file(root, files, "fastq_r2"),
        vcf=_optional_dataset_file(root, files, "vcf"),
        vcf_index=_optional_dataset_file(root, files, "vcf_index"),
        targets=_optional_dataset_file(root, files, "targets"),
        targets_index=_optional_dataset_file(root, files, "targets_index"),
        default_region=manifest.get("default_region")
        if isinstance(manifest.get("default_region"), str)
        else None,
        region_safe=bool(manifest.get("region_safe", False)),
        manifest=manifest,
    )
    _validate_real_benchmark_dataset(dataset)
    return dataset


def _prepare_direct_real_benchmark_dataset(
    args: argparse.Namespace,
    dataset_dir: Path,
    outdir: Path,
    spec: BenchmarkDatasetSpec,
) -> BenchmarkDataset:
    cache_root = _real_dataset_cache_dir(args, outdir) / spec.dataset_id
    cache_root.mkdir(parents=True, exist_ok=True)
    cached_files: dict[str, Path] = {}
    for remote in spec.remote_files:
        cached_files[remote.role] = _cached_remote_dataset_file(remote, cache_root)

    if spec.derived_alignment:
        _prepare_derived_alignment(spec.derived_alignment, cached_files, cache_root)

    _write_direct_dataset_manifest(spec, cache_root, cached_files)

    _link_cached_dataset_manifest(
        cache_root, dataset_dir / f"{spec.tag}-cache-root.txt"
    )
    mark_cache_item_used(cache_root)
    return _load_real_benchmark_dataset(cache_root)


def _normalized_dataset_sha256(args: argparse.Namespace) -> str | None:
    value = getattr(args, "dataset_sha256", None)
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def _download_filename(url: str) -> str:
    filename = Path(url.split("?", 1)[0]).name
    if not filename:
        raise WGSExtractError(f"Dataset URL must end with a filename: {url}")
    return filename


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
