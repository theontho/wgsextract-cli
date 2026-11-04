"""`info chip`: identify the vendor, chip and revision of a microarray raw-data file."""

from pathlib import Path
from typing import Any

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.info import ChipParams
from wgsextract_cli.core.microarray_revisions import (
    Detection,
    Revision,
    detect,
    load_catalogue,
    read_raw_export,
)
from wgsextract_cli.core.microarray_utils import (
    _PACKAGED_TEMPLATES,
    _resolve_templates_roots,
)
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.results import current_result, human_output
from wgsextract_cli.core.variant_files import verify_paths_exist

_RELATION = {
    "exact": "identical markers and coordinates",
    "same-markers": "identical markers, different coordinates",
    "closest": "closest template",
}


def _template_roots(ref: str | None) -> list[str]:
    searches = [
        str(Path(ref).parent if Path(ref).is_file() else ref) if ref else None,
        resolve_reference_library(),
        str(_PACKAGED_TEMPLATES),
    ]
    return _resolve_templates_roots([s for s in searches if s])


def _template_text(revision: Revision) -> str:
    if not revision.template:
        return "none packaged or in the reference library"
    text = (
        f"{revision.template} (--formats {revision.formats}), "
        f"{_RELATION[revision.template_relation or 'closest']}"
    )
    if revision.template_relation == "closest":
        text += (
            f": revision has {revision.template_added:,} markers the template lacks"
            f" and lacks {revision.template_removed:,} of its markers"
        )
    return text


def _label(revision: Revision) -> str:
    return f"{revision.vendor} {revision.chip}, {revision.revision}"


def _revision_json(revision: Revision) -> dict[str, Any]:
    return {
        "id": revision.id,
        "vendor": revision.vendor,
        "chip": revision.chip,
        "revision": revision.revision,
        "build": revision.build,
        "markers": revision.markers,
        "kind": revision.kind,
        "exports": revision.exports,
        "period": revision.period,
        "template": (
            {
                "name": revision.template,
                "formats": revision.formats,
                "relation": revision.template_relation,
                "added": revision.template_added,
                "removed": revision.template_removed,
            }
            if revision.template
            else None
        ),
        "source": revision.source,
    }


def _detection_json(detection: Detection) -> dict[str, Any]:
    export = detection.export
    return {
        "file": {
            "vendor": export.vendor,
            "declared_chip": export.declared_chip,
            "declared_build": export.declared_build,
            "export_month": export.export_month,
            "layout": export.layout,
            "markers": export.fingerprint.markers,
            "ids_sha256": export.fingerprint.ids_sha256,
            "sites_sha256": export.fingerprint.sites_sha256,
        },
        "confidence": detection.confidence,
        "ambiguous": detection.ambiguous,
        "candidates": [
            {
                "score": round(candidate.score, 6),
                "match": candidate.match,
                "estimated": not candidate.exact_score,
                "revision": _revision_json(candidate.revision),
            }
            for candidate in detection.candidates
        ],
    }


def _render(detection: Detection) -> str:
    export = detection.export
    fp = export.fingerprint
    declared = ", ".join(
        f"{name} {value}"
        for name, value in (
            ("vendor", export.vendor),
            ("chip", export.declared_chip),
            ("build", export.declared_build),
            ("exported", export.export_month),
        )
        if value
    )
    lines = [
        f"Markers:     {fp.markers:,} ({export.layout})",
        f"Header:      {declared or 'no vendor, chip or build declared'}",
    ]
    best = detection.best
    if best is None:
        return "\n".join([*lines, "Detected:    no catalogued revision"])
    revision = best.revision
    match = {
        "exact": "identical marker set and coordinates",
        "markers": "identical marker set; coordinates differ (another build?)",
        "closest": "closest catalogued revision"
        + (" (estimated)" if not best.exact_score else ""),
    }[best.match]
    lines += [
        f"Detected:    {_label(revision)} [{revision.id}]",
        f"Match:       {match}",
        f"Confidence:  {detection.confidence:.3f}"
        + (" (ambiguous: next candidate is as close)" if detection.ambiguous else ""),
        f"Build:       {revision.build} (revision); "
        f"{export.declared_build or 'not declared'} (file)",
        f"Template:    {_template_text(revision)}",
    ]
    if best.match != "exact" and revision.markers != fp.markers:
        lines.append(
            f"Revision has {revision.markers:,} markers; this file has {fp.markers:,}."
        )
    others = detection.candidates[1:]
    if others:
        lines.append("Next candidates:")
        lines += [
            f"  {c.score:.3f}{'~' if not c.exact_score else ' '} "
            f"{_label(c.revision)} [{c.revision.id}]"
            for c in others
        ]
    return "\n".join(lines)


def _render_catalogue(revisions: tuple[Revision, ...]) -> str:
    lines = []
    for revision in revisions:
        observed = (
            f"; {revision.exports} public exports {revision.period or ''}".rstrip()
            if revision.exports
            else ""
        )
        lines.append(
            f"{revision.id}: {_label(revision)}; {revision.markers:,} markers, "
            f"{revision.build}{observed}"
        )
        lines.append(f"    template: {_template_text(revision)}")
        if not revision.detectable:
            lines.append(
                "    composite marker list, not a chip; not used for detection"
            )
    return "\n".join(lines)


def run_chip(params: ChipParams) -> None:
    result = current_result()
    if params.list_revisions:
        revisions = load_catalogue()
        result.set_details({"revisions": [_revision_json(r) for r in revisions]})
        result.add_metrics(revisions=len(revisions))
        human_output(_render_catalogue(revisions))
        return
    input_file = str(params.input)
    verify_paths_exist({"--input": input_file})
    export = read_raw_export(input_file)
    detection = detect(
        export, template_roots=_template_roots(str(params.ref) if params.ref else None)
    )
    result.set_details(_detection_json(detection))
    best = detection.best
    result.add_metrics(
        markers=export.fingerprint.markers,
        revision=best.revision.id if best else None,
        match=best.match if best else None,
        confidence=round(detection.confidence, 6),
        ambiguous=detection.ambiguous,
    )
    if detection.ambiguous:
        result.warn(
            "chip_ambiguous",
            "The next candidate revision matches this file as closely as the best one.",
        )
    human_output(_render(detection))


run = cli_handler(ChipParams, run_chip)
