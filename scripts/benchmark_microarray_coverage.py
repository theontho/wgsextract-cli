"""Microarray accuracy versus coverage: downsample a deep WGS and score its kits.

Downsamples one alignment with `samtools view -s SEED.FRACTION` (one seed, so
the read sets are nested) to each requested mean autosomal depth, runs
`wgsextract microarray` on every copy and scores its kits against an
independently measured chip VCF of the same sample, by marker identity
(allele-exact, as scripts/benchmark_microarray_chip.py does). Results are
broken down by autosomes/X/Y/MT and by the truth genotype class, together with
the depths, homozygosity, no-call rates and QC warnings the CLI reported;
low-coverage warnings are re-evaluated from the measured depths with the
current thresholds, so cached results follow a changed qc_min_*_depth.

Outputs (raw kits, truth and logs) stay under --outdir; the summary JSON and
Markdown contain aggregate counts only. Re-running with the same inputs and
options resumes (completed depths are skipped); other inputs need a new
--outdir. Run each step under an agent-coord lease.
"""

import argparse
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wgsextract_cli.commands.benchmark.chip import (
    CONSUMER_FORMATS,
    ChipCall,
    read_chip_export,
)
from wgsextract_cli.core.config import reload_settings, set_config_path
from wgsextract_cli.core.genotype_qc import (
    AUTOSOMES,
    low_depth_targets,
    region_targets,
    target_depths,
)
from wgsextract_cli.core.microarray_utils import get_template_format, is_autosome
from wgsextract_cli.core.reference_assets import input_stem

DEFAULT_DEPTHS = "30,20,15,10,8,6,5,4,3,2,1"
GROUPS = ("autosomes", "X", "Y", "MT")
CLASSES = ("hom_ref", "het", "hom_alt")


def group_of(chrom: str) -> str:
    return "autosomes" if is_autosome(chrom) else chrom


@dataclass
class Counts:
    eligible: int = 0
    called: int = 0
    concordant: int = 0
    nocall: int = 0
    # Truth heterozygous, called homozygous: the typical low-coverage error.
    het_called_hom: int = 0
    # Truth homozygous, called heterozygous.
    hom_called_het: int = 0

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            **asdict(self),
            "call_rate": self.called / self.eligible if self.eligible else None,
            "concordance": self.concordant / self.called if self.called else None,
            "accuracy": self.concordant / self.eligible if self.eligible else None,
        }


@dataclass
class Score:
    total: Counts = field(default_factory=Counts)
    groups: dict[str, Counts] = field(default_factory=dict)
    # Per group, by the truth genotype class.
    classes: dict[str, dict[str, Counts]] = field(default_factory=dict)
    # The kit's own autosomal rates, which genotype QC warns on.
    kit_autosomal_sites: int = 0
    kit_autosomal_called: int = 0
    kit_autosomal_het: int = 0

    def as_dict(self) -> dict[str, object]:
        called = self.kit_autosomal_called
        return {
            "total": self.total.as_dict(),
            "groups": {k: v.as_dict() for k, v in self.groups.items()},
            "classes": {
                group: {k: v.as_dict() for k, v in sorted(counts.items())}
                for group, counts in self.classes.items()
            },
            "kit_autosomal_sites": self.kit_autosomal_sites,
            "kit_autosomal_no_call_rate": (
                1 - called / self.kit_autosomal_sites
                if self.kit_autosomal_sites
                else None
            ),
            "kit_autosomal_homozygosity": (
                1 - self.kit_autosomal_het / called if called else None
            ),
        }


def is_het(genotype: str) -> bool:
    return len(genotype) == 2 and genotype[0] != genotype[1]


def score(
    kit: dict[str, ChipCall], truth: dict[str, ChipCall], classes: dict[str, str]
) -> Score:
    """Score called and no-called kit markers present in the truth with the same
    chromosome. Eligibility never depends on the kit's genotype."""
    result = Score()
    for marker, observed in kit.items():
        if is_autosome(observed.chromosome):
            result.kit_autosomal_sites += 1
            if observed.genotype:
                result.kit_autosomal_called += 1
                result.kit_autosomal_het += is_het(observed.genotype)
        expected = truth.get(marker)
        if (
            expected is None
            or expected.genotype is None
            or expected.chromosome != observed.chromosome
        ):
            continue
        group = group_of(expected.chromosome)
        buckets = [
            result.total,
            result.groups.setdefault(group, Counts()),
            result.classes.setdefault(group, {}).setdefault(classes[marker], Counts()),
        ]
        got, want = observed.genotype, expected.genotype
        agrees = got == want or (
            got is not None
            and group != "autosomes"
            and (got * 2 == want or want * 2 == got)
        )
        for counts in buckets:
            counts.eligible += 1
            if got is None:
                counts.nocall += 1
                continue
            counts.called += 1
            counts.concordant += int(agrees)
            if not agrees:
                counts.het_called_hom += int(is_het(want) and not is_het(got))
                counts.hom_called_het += int(not is_het(want) and is_het(got))
    return result


def run(cmd: list[str], log: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    if log is None:
        subprocess.run(cmd, check=True)
        return
    with log.open("w", encoding="utf-8", newline="\n") as stream:
        subprocess.run(cmd, check=True, stdout=stream, stderr=subprocess.STDOUT)


def genotype_class(gt: str) -> str | None:
    alleles = gt.replace("|", "/").split("/")
    if not alleles or any(not a.isdigit() for a in alleles):
        return None
    if len(set(alleles)) > 1:
        return "het"
    return "hom_ref" if alleles[0] == "0" else "hom_alt"


def build_truth(
    vcf: Path, sample: str, outdir: Path
) -> tuple[dict[str, ChipCall], dict[str, str]]:
    """Truth as a CombinedKit-layout TSV, so duplicate rsIDs with conflicting
    genotypes are quarantined exactly as the chip scorer does."""
    table = outdir / "truth.tsv"
    if not table.exists():
        query = subprocess.run(
            ["bcftools", "query", "-s", sample]
            + ["-f", "%ID\t%CHROM\t%POS[\t%GT\t%TGT]\n", str(vcf)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        rows = []
        for line in query.splitlines():
            rsid, chrom, pos, gt, tgt = line.split("\t")
            if rsid.startswith("rs") and genotype_class(gt):
                rows.append(f"{rsid}\t{chrom}\t{pos}\t{gt}\t{tgt}\n")
        partial = table.with_suffix(".partial")
        partial.write_text("".join(rows), encoding="utf-8", newline="\n")
        partial.replace(table)
    classes: dict[str, str] = {}
    kit_rows = []
    for line in table.read_text(encoding="utf-8").splitlines():
        rsid, chrom, pos, gt, tgt = line.split("\t")
        cls = genotype_class(gt) or ""
        classes[rsid] = cls if classes.get(rsid, cls) == cls else ""
        kit_rows.append(f"{rsid}\t{chrom}\t{pos}\t{tgt}\n")
    kit_table = outdir / "truth.combinedkit.tsv"
    kit_table.write_text("".join(kit_rows), encoding="utf-8", newline="\n")
    truth = read_chip_export(kit_table, "CombinedKit")
    return {k: v for k, v in truth.items() if classes.get(k)}, classes


def downsample(
    source: Path, ref: Path, fraction: float, seed: int, out: Path, threads: int
) -> None:
    if out.exists() and Path(f"{out}.crai").exists():
        return
    partial = out.with_name(out.name + ".partial.cram")
    run(
        ["samtools", "view", "-C", "-T", str(ref), "-@", str(threads)]
        + ["-s", f"{seed}.{fraction_digits(fraction)}"]
        + ["-o", str(partial), str(source)]
    )
    run(["samtools", "index", "-@", str(threads), str(partial)])
    Path(f"{partial}.crai").replace(f"{out}.crai")
    partial.replace(out)


def fraction_digits(fraction: float) -> str:
    """The digits after the point in samtools' SEED.FRACTION."""
    digits = f"{fraction:.6f}".split(".")[1].rstrip("0")
    if not digits or not 0 < fraction < 1:
        raise ValueError(f"fraction must be in (0, 1): {fraction}")
    return digits


def kit_paths(outdir: Path, stem: str, names: list[str]) -> dict[str, Path]:
    paths = {}
    for name in names:
        info = get_template_format(name)
        suffix = info["suffix"] if info else ".txt"
        found = list(outdir.rglob(f"{stem}_{name}{suffix}"))
        if len(found) != 1:
            raise RuntimeError(f"Expected one {name} export in {outdir}: {found}")
        paths[name] = found[0]
    return paths


def warnings_of(outdir: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for manifest in outdir.rglob("*.manifest.json"):
        document = json.loads(manifest.read_text(encoding="utf-8"))
        found.extend(document.get("warnings") or [])
    return found


def current_warnings(result: dict[str, Any]) -> list[str]:
    """The run's warnings, its low-coverage ones re-judged from its measured
    depths against the thresholds the CLI uses now."""
    kept = {w for w in result["warnings"] if not w.startswith("low_coverage")}
    low = low_depth_targets(result["measured_depth"] or {}, region_targets(None))
    return sorted(kept | {f"low_coverage:{target}" for target, *_ in low})


def run_depth(
    args: argparse.Namespace,
    label: str,
    alignment: Path,
    truth: dict[str, ChipCall],
    classes: dict[str, str],
) -> dict[str, Any]:
    workdir = args.outdir / label
    result_file = workdir / "result.json"
    if result_file.exists():
        cached: dict[str, Any] = json.loads(result_file.read_text("utf-8"))
        return cached
    workdir.mkdir(parents=True, exist_ok=True)
    depths = target_depths(str(alignment), ref=str(args.ref))
    kits = workdir / "kits"
    formats = ",".join(
        n.lower().replace("23andme_snps_api", "23andme_api") for n in args.formats
    )
    command = [sys.executable, "-m", "wgsextract_cli.main"]
    if args.config:
        command += ["--config", str(args.config)]
    command += ["microarray", "--input", str(alignment), "--ref", str(args.ref)]
    command += ["--outdir", str(kits), "--threads", str(args.threads)]
    command += ["--formats", formats]
    started = time.monotonic()
    run(command, workdir / "microarray.log")
    elapsed = time.monotonic() - started
    stem = input_stem(alignment)
    names = ["CombinedKit", *args.formats]
    scores = {
        name: score(read_chip_export(path, name), truth, classes).as_dict()
        for name, path in kit_paths(kits, stem, names).items()
    }
    result: dict[str, Any] = {
        "label": label,
        "measured_depth": depths[0] if depths else None,
        "microarray_seconds": round(elapsed, 1),
        "warnings": sorted(
            {
                f"{w.get('code')}:{(w.get('details') or {}).get('target', '')}".rstrip(
                    ":"
                )
                for w in warnings_of(kits)
            }
        ),
        "kits": scores,
    }
    result_file.write_text(json.dumps(result, indent=2) + "\n", "utf-8")
    return result


def pct(value: object, digits: int = 2) -> str:
    return f"{100 * value:.{digits}f}" if isinstance(value, float) else "n/a"


def rate(counts: dict[str, Any], key: str) -> float | None:
    return counts[key] / counts["eligible"] if counts.get("eligible") else None


def hom_concordance(kit: dict[str, Any], group: str) -> float | None:
    """Called concordance where the truth is homozygous: a man's chip "hets" on
    chrX/chrY are mostly X/Y-homologous probes, not two alleles."""
    homs = [kit["classes"].get(group, {}).get(c, {}) for c in ("hom_ref", "hom_alt")]
    called = sum(c.get("called", 0) for c in homs)
    return sum(c.get("concordant", 0) for c in homs) / called if called else None


def markdown(summary: dict[str, Any], kit_name: str) -> str:
    """One row per depth: the vendor kit's autosomal accuracy and no-calls, its
    chrX/chrY concordance at truth-homozygous sites, and what QC reported."""
    lines = [
        f"{kit_name} markers on the truth chip; CombinedKit homozygosity is what "
        "genotype QC checks.\n",
        "| Depth | Measured | Called concordance % | No-call % | Het correct % "
        "| Het called hom % | Hom-alt correct % | Hom-ref correct % "
        "| chrX / chrY concordance % | CombinedKit homozygosity % | Warnings |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in summary["depths"]:
        kit = row["kits"][kit_name]
        auto = kit["groups"].get("autosomes", {})
        cls = kit["classes"].get("autosomes", {})
        het = cls.get("het", {})
        sex = " / ".join(pct(hom_concordance(kit, group)) for group in ("X", "Y"))
        depth = (row["measured_depth"] or {}).get(AUTOSOMES)
        no_call = rate(auto, "nocall")
        measured = f"{depth:.1f}x" if depth else "n/a"
        lines.append(
            f"| {row['label']} | {measured} | {pct(auto.get('concordance'))} | "
            f"{pct(no_call)} | {pct(het.get('accuracy'))} | "
            f"{pct(rate(het, 'het_called_hom'))} | "
            f"{pct(cls.get('hom_alt', {}).get('accuracy'))} | "
            f"{pct(cls.get('hom_ref', {}).get('accuracy'))} | {sex} | "
            f"{pct(row['kits']['CombinedKit']['kit_autosomal_homozygosity'], 1)} | "
            f"{', '.join(row['warnings']) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def span(values: list[float | None], digits: int = 2) -> str:
    known = [v for v in values if v is not None]
    if not known:
        return "n/a"
    low, high = pct(min(known), digits), pct(max(known), digits)
    return low if low == high else f"{low}–{high}"


def kits_markdown(summary: dict[str, Any]) -> str:
    """Ranges across the vendor kits: their autosomal accuracy on truth markers
    and their own autosomal no-call rates, which genotype QC checks."""
    lines = [
        "| Depth | Kits | Called concordance % | No-call % on truth markers "
        "| Kit no-call % (all markers) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["depths"]:
        kits = [v for k, v in row["kits"].items() if k != "CombinedKit"]
        autos = [k["groups"].get("autosomes", {}) for k in kits]
        lines.append(
            f"| {row['label']} | {len(kits)} | "
            f"{span([a.get('concordance') for a in autos])} | "
            f"{span([rate(a, 'nocall') for a in autos])} | "
            f"{span([k['kit_autosomal_no_call_rate'] for k in kits])} |"
        )
    return "\n".join(lines) + "\n"


def run_identity(args: argparse.Namespace) -> dict[str, Any]:
    """What cached truth, downsampled copies and scores depend on."""

    def file(path: Path | None) -> list[Any] | None:
        return None if path is None else [str(path.resolve()), path.stat().st_size]

    return {
        "input": file(args.input),
        "ref": file(args.ref),
        "truth_vcf": file(args.truth_vcf),
        "config": file(args.config),
        "sample": args.sample,
        "seed": args.seed,
        "formats": args.formats,
    }


def check_resume(outdir: Path, identity: dict[str, Any]) -> None:
    """Refuse to mix results from different inputs or options in one --outdir."""
    marker = outdir / "run.json"
    if marker.exists():
        previous = json.loads(marker.read_text("utf-8"))
        if previous != identity:
            changed = sorted(k for k in identity if previous.get(k) != identity[k])
            raise SystemExit(
                f"{outdir} holds results for different {', '.join(changed)}; "
                "use a new --outdir."
            )
    else:
        marker.write_text(json.dumps(identity, indent=2) + "\n", "utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Deep BAM/CRAM")
    parser.add_argument("--ref", type=Path, required=True)
    parser.add_argument("--truth-vcf", type=Path, required=True)
    parser.add_argument("--sample", required=True, help="Sample in --truth-vcf")
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--depths", default=DEFAULT_DEPTHS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument(
        "--formats",
        default=",".join(CONSUMER_FORMATS),
        help="Consumer formats to score besides CombinedKit (default: all)",
    )
    parser.add_argument(
        "--report-kit",
        default="23andMe_V5",
        help="Vendor kit for the Markdown table (default: 23andMe_V5)",
    )
    parser.add_argument(
        "--keep-downsampled",
        action="store_true",
        help="Keep each downsampled CRAM after scoring it",
    )
    args = parser.parse_args()
    args.formats = [f for f in args.formats.split(",") if f]
    unknown = [f for f in args.formats if f not in CONSUMER_FORMATS]
    if args.report_kit not in args.formats:
        parser.error("--report-kit must be one of --formats")
    if unknown:
        parser.error(f"Unknown formats: {', '.join(unknown)}")
    set_config_path(args.config)
    reload_settings()
    args.outdir.mkdir(parents=True, exist_ok=True)
    check_resume(args.outdir, run_identity(args))

    measured = target_depths(str(args.input), ref=str(args.ref))
    if not measured or not measured[0].get(AUTOSOMES):
        parser.error(f"Cannot measure the mean autosomal depth of {args.input}")
    source_depth = measured[0][AUTOSOMES]
    print(f"Source autosomal depth {source_depth:.2f}x ({measured[1]})", flush=True)
    truth, classes = build_truth(args.truth_vcf, args.sample, args.outdir)
    print(f"Truth markers: {len(truth):,}", flush=True)

    depths = sorted({float(d) for d in args.depths.split(",")}, reverse=True)
    plan: list[tuple[str, float]] = [("full", 1.0)]
    for depth in depths:
        if depth < source_depth:
            plan.append((f"{depth:g}x", depth / source_depth))
        else:
            print(f"Skipping {depth:g}x: not below the source depth", flush=True)
    rows = []
    # samtools keeps a read when its seeded name hash is below the fraction, so
    # sampling an earlier copy with the same seed and absolute fraction selects
    # exactly the reads sampling the source would, from fewer reads.
    source = args.input
    for label, fraction in plan:
        alignment = args.input
        if fraction < 1:
            alignment = args.outdir / label / f"ds{label}.cram"
            if not (args.outdir / label / "result.json").exists():
                alignment.parent.mkdir(parents=True, exist_ok=True)
                downsample(
                    source, args.ref, fraction, args.seed, alignment, args.threads
                )
        result = run_depth(args, label, alignment, truth, classes)
        result["fraction"] = round(fraction, 6)
        result["warnings"] = current_warnings(result)
        rows.append(result)
        print(f"{label}: scored", flush=True)
        if alignment.exists():
            if source != args.input and not args.keep_downsampled:
                for path in (source, Path(f"{source}.crai")):
                    path.unlink(missing_ok=True)
            source = alignment
    if source != args.input and not args.keep_downsampled:
        for path in (source, Path(f"{source}.crai")):
            path.unlink(missing_ok=True)

    truth_classes = Counter(
        group_of(call.chromosome) + ":" + classes[marker]
        for marker, call in truth.items()
    )
    summary = {
        "sample": args.sample,
        "seed": args.seed,
        "source_depth": measured[0],
        "truth_markers": dict(sorted(truth_classes.items())),
        "depths": rows,
    }
    (args.outdir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", "utf-8"
    )
    table = markdown(summary, args.report_kit) + "\n" + kits_markdown(summary)
    (args.outdir / "summary.md").write_text(table, "utf-8")
    print(f"Wrote {args.outdir / 'summary.md'}", flush=True)


if __name__ == "__main__":
    main()
