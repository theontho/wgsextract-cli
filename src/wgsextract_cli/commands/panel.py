"""``panel bed``: genes and presets to a BED of padded, merged target regions."""

import argparse
import logging
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.panel import PanelBedParams
from wgsextract_cli.core import panel
from wgsextract_cli.core.atomic_output import safe_output
from wgsextract_cli.core.builds import build_from_chr1_length
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.panel_presets import PRESETS, preset_genes
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.results import Scalar, current_result, human_output, warn
from wgsextract_cli.core.utils import UsageError


def _list_presets() -> None:
    for preset in PRESETS.values():
        genes = list(preset_genes(preset))
        genes += [f"{s.name} ({s.first}..{s.last})" for s in preset.spans]
        count = f"{len(genes)} {'locus' if len(genes) == 1 else 'loci'}"
        human_output(f"{preset.name}: {preset.title}, {count}")
        human_output(f"  Source: {preset.source}")
        if preset.note:
            human_output(f"  {preset.note}")
        human_output(f"  {', '.join(genes)}")


def _reference(params: PanelBedParams) -> tuple[dict[str, int], str | None]:
    """The --ref FASTA's contig lengths and build (none for a library folder)."""
    if params.ref is None:
        return {}, None
    ref = Path(params.ref).expanduser()
    if ref.is_dir():
        logging.info(
            "--ref %s is a folder; writing chrN names. Give the FASTA MinKNOW "
            "uses to match its contig names.",
            ref,
        )
        return {}, None
    lengths = read_fai_lengths(ref)
    if not lengths:
        raise UsageError(
            f"--ref {ref} has no readable .fai index; make one with "
            f"'samtools faidx {ref}'."
        )
    return lengths, build_from_chr1_length(lengths)


def run_panel_bed(params: PanelBedParams) -> None:
    if params.list_presets:
        _list_presets()
        return
    fai, ref_build = _reference(params)
    if params.build and ref_build and params.build != ref_build:
        raise UsageError(f"--ref is {ref_build}, not --build {params.build}.")
    build = params.build or ref_build or "hg38"
    notes = panel.load_annotations(build)

    loci: list[panel.Locus] = []
    for name in params.presets:
        loci += panel.preset_loci(name, notes)
    names = [g for item in params.genes for g in item.replace(",", " ").split()]
    if params.gene_file:
        names += panel.read_gene_file(Path(params.gene_file))
    resolved = panel.resolve_genes(names, notes)
    loci += resolved.loci
    for given, symbol in resolved.aliases.items():
        warn(
            "gene_alias",
            f"{given} is an alias of {symbol}; targeting {symbol}.",
            gene=given,
            symbol=symbol,
        )
    if not loci:
        raise UsageError("No genes to target.")

    lengths = dict(notes.contigs)
    contig_names = {c: c for c in lengths}
    if fai:
        contig_names, differ = panel.reference_contigs(fai, notes)
        wrong = [c for c in differ if c != "chrM"]
        if wrong:
            raise UsageError(
                f"--ref doesn't match {build}: {', '.join(wrong)} "
                f"{'has' if len(wrong) == 1 else 'have'} other lengths."
            )
        missing = sorted({x.symbol for x in loci if x.chrom not in contig_names})
        if missing:
            warn(
                "panel_contig_missing",
                f"--ref lacks the contigs of {', '.join(missing)}; left out.",
                genes=",".join(missing),
            )
            loci = [x for x in loci if x.chrom in contig_names]
        if "chrM" in differ:
            # Not the rCRS (e.g. hg19's Yoruba chrM): target the whole contig.
            lengths["chrM"] = fai[contig_names["chrM"]]
            loci = [
                panel.Locus("chrM", 0, lengths["chrM"], x.symbol)
                if x.chrom == "chrM"
                else x
                for x in loci
            ]
            if any(x.chrom == "chrM" for x in loci):
                warn(
                    "panel_chrm_not_rcrs",
                    f"--ref's {contig_names['chrM']} is not the rCRS; "
                    "targeting all of it.",
                )
    pad = round(params.pad_kb * 1000)
    targets = panel.build_targets(loci, pad, lengths)
    target_bases = sum(t.end - t.start for t in targets)
    genome = sum(notes.contigs.values())
    fraction = target_bases / genome

    outdir = Path(params.outdir or Path.cwd()).expanduser().absolute()
    outdir.mkdir(parents=True, exist_ok=True)
    label = "_".join([*dict.fromkeys(params.presets), *(["genes"] if names else [])])
    pad_tag = f"{params.pad_kb:g}".replace(".", "p")
    bed_path = outdir / f"panel_{label}_{build}_pad{pad_tag}kb.bed"
    with (
        safe_output(bed_path) as staged,
        open(staged, "w", encoding="utf-8", newline="\n") as stream,
    ):
        stream.write(panel.bed_text(targets, contig_names))

    genes = len({x.symbol for x in loci})
    result = current_result()
    metrics: dict[str, Scalar] = {
        "build": build,
        "loci": genes,
        "targets": len(targets),
        "target_bases": target_bases,
        "genome_fraction": round(fraction, 6),
        "pad_bases": pad,
    }
    result.add_output(bed_path, "panel_bed", kind="bed", metrics=metrics)
    result.add_metrics(**metrics)
    result.set_manifest_stem(bed_path.with_suffix(""))

    human_output(
        f"{genes} {'locus' if genes == 1 else 'loci'} in {len(targets)} "
        f"region{'s' * (len(targets) != 1)} of {build} (±{params.pad_kb:g} kb): "
        f"{target_bases / 1e6:.2f} Mb, {fraction:.2%} of the "
        f"{genome / 1e9:.2f} Gb genome."
    )
    if fraction > panel.WARN_FRACTION:
        warn(
            "panel_too_large",
            f"The panel is {fraction:.1%} of the genome; adaptive sampling "
            f"enriches poorly above {panel.WARN_FRACTION:.0%} (ONT). Use fewer "
            "genes or less padding.",
            genome_fraction=round(fraction, 6),
        )
    elif fraction > panel.RECOMMENDED_FRACTION:
        human_output(
            f"Enrichment is best under {panel.RECOMMENDED_FRACTION:.0%} of the "
            "genome; consider fewer genes or less padding."
        )
    human_output(f"BED: {bed_path}")
    if not fai:
        human_output(
            "Contigs are named chr1..chrM; give --ref with the FASTA MinKNOW uses "
            "if it names them otherwise."
        )


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "panel", parents=[base_parser], help=CLI_HELP["cmd_panel"]
    )
    subs = parser.add_subparsers(dest="panel_cmd", required=True)
    sub = subs.add_parser(
        "bed",
        parents=[base_parser],
        help=CLI_HELP["cmd_panel-bed"],
        description=CLI_HELP["cmd_panel-bed"],
    )
    add_arguments(sub, PanelBedParams)
    sub.set_defaults(func=cli_handler(PanelBedParams, run_panel_bed))


__all__ = ["register", "run_panel_bed"]
