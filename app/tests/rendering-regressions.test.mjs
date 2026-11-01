import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
  checkedOptionsForContext, hydrateRows, initialCheckedOptions, initialFieldValues,
  parseFlatToml, renderedCommand,
} from "../dist/shared/rendering.js";

test("parses flat TOML inline comments outside quoted strings", () => {
  assert.deepEqual({ ...parseFlatToml('plain = value # comment\nquoted = "value # not comment" # comment\n') }, {
    plain: "value",
    quoted: "value # not comment",
  });
});

test("hydrates whitespace-only row metadata as empty", () => {
  const rows = hydrateRows({ rowTemplate: { id: "one", title: "One", status: "   " }, items: [{}] });

  assert.equal(rows[0].status, undefined);
});

const wgsPages = ["microarray", "extract", "fastq", "info-bam", "ancestry", "vcf", "settings", "library"]
  .map((id) => JSON.parse(readFileSync(new URL(`../../examples/WGSExtract/pages/${id}.json`, import.meta.url), "utf8")));
const wgsManifest = { pages: wgsPages };

function wgsCommand(pageID, actionID, fieldValues = {}, checkedOptions = {}) {
  const page = wgsPages.find((item) => item.id === pageID);
  const action = page.sections.flatMap((section) => section.actions ?? []).find((item) => item.id === actionID);
  return renderedCommand(action.command, {
    bundleRootPath: "bundle",
    fieldValues: { ...initialFieldValues(wgsManifest), bam_path: "sample.bam", ref_fasta: "ref.fa", ...fieldValues },
    checkedOptions: checkedOptionsForContext({ ...initialCheckedOptions(wgsManifest), ...checkedOptions }),
  }).arguments;
}

test("WGSExtract new options do not change default argv or selected formats", () => {
  assert.deepEqual(wgsCommand("microarray", "microarray-generate"), [
    "microarray", "--input", "sample.bam", "--ref", "ref.fa", "--formats", "23andme_v35,23andme_v5,all",
  ]);
  for (const [page, action, flag] of [
    ["extract", "ydna-bam", "--poz"], ["extract", "ydna-vcf", "--ybrowse"],
    ["extract", "unmapped", "--fastq"], ["vcf", "vcf-gatk", "--scatter"],
    ["info-bam", "realign", "--keep-fastq"], ["ancestry", "run-yleaf", "--tree-links"],
    ["fastq", "multiqc", "--force"],
  ]) {
    assert.ok(!wgsCommand(page, action).includes(flag));
  }
});

test("WGSExtract opt-in policies render flags and preserve manifest paths with spaces", () => {
  const args = wgsCommand("microarray", "microarray-generate", {
    microarray_min_gq: "20", microarray_min_dp: "5",
    aadr_sample_id: "research_sample", aadr_sex: "U",
    indel_format: "myheritage_gsa_2025", indel_manifest: "manifests/matching chip.csv",
  }, {
    microarray_formats: ["myheritage_gsa_2025", "aadr_1240k", "aadr_ho", "aadr_1240k_ho"],
    microarray_strand_ambiguous: ["no-call"], microarray_resolve_rsids: ["--resolve-rsids"],
    microarray_absent_no_call: ["--no-assume-hom-ref"],
    microarray_exclude_filtered: ["--exclude-filtered"], microarray_call_reasons: ["--call-reasons"],
  });

  test("WGSExtract AADR 2M extraction and metadata installation remain separate opt-ins", () => {
    const page = wgsPages.find((item) => item.id === "microarray");
    const formats = page.sections.flatMap((section) => section.controls ?? [])
      .find((control) => control.id === "microarray_formats");
    assert.ok(formats.options.some((option) => option.id === "aadr_2m"), "2M is selectable");
    assert.deepEqual(wgsCommand("microarray", "microarray-generate", {}, {
      microarray_formats: ["aadr_2m"],
    }), [
      "microarray", "--input", "sample.bam", "--ref", "ref.fa", "--formats", "aadr_2m",
    ]);
    assert.deepEqual(wgsCommand("library", "aadr-2m-install", { ref_path: "reference library" }), [
      "ref", "aadr", "--install", "--ref", "reference library",
    ]);
    assert.ok(!wgsCommand("microarray", "microarray-generate").includes("aadr_2m"));
  });
  assert.deepEqual(args.slice(5), [
    "--formats", "aadr_1240k,aadr_1240k_ho,aadr_ho,myheritage_gsa_2025",
    "--min-gq", "20", "--min-dp", "5", "--strand-ambiguous", "no-call",
    "--resolve-rsids", "--no-assume-hom-ref", "--exclude-filtered", "--call-reasons",
    "--aadr-sample-id", "research_sample", "--aadr-sex", "U",
    "--indel-manifest", "myheritage_gsa_2025=manifests/matching chip.csv",
  ]);
  assert.ok(!wgsCommand("microarray", "microarray-generate", { indel_format: "myheritage_gsa_2025" }).includes("--indel-manifest"));
  assert.ok(!wgsCommand("microarray", "microarray-generate", { indel_manifest: "chip.csv" }).includes("--indel-manifest"));
});

test("WGSExtract targeted opt-ins are restricted to the relevant commands", () => {
  const choices = {
    extract_poz: ["--poz"], extract_ybrowse: ["--ybrowse"], extract_unmapped_fastq: ["--fastq"],
    gatk_scatter: ["--scatter"], realign_keep_fastq: ["--keep-fastq"], lineage_tree_links: ["--tree-links"],
  };
  for (const [page, action, flag] of [
    ["extract", "ydna-bam", "--poz"], ["extract", "ydna-vcf", "--ybrowse"],
    ["extract", "unmapped", "--fastq"], ["vcf", "vcf-gatk", "--scatter"],
    ["info-bam", "realign", "--keep-fastq"], ["ancestry", "run-yleaf", "--tree-links"],
    ["ancestry", "run-haplogrep", "--tree-links"],
  ]) {
    assert.ok(wgsCommand(page, action, {}, choices).includes(flag));
  }
  assert.ok(!wgsCommand("extract", "custom", {}, choices).some((arg) => ["--poz", "--ybrowse", "--fastq"].includes(arg)));
  assert.ok(!wgsCommand("vcf", "vcf-snp", {}, choices).includes("--scatter"));
  assert.deepEqual(wgsCommand("fastq", "multiqc", {
    multiqc_input: "reports with spaces", out_dir: "new reports",
  }, { multiqc_force: ["--force"] }), [
    "qc", "multiqc", "--input", "reports with spaces", "--outdir", "new reports", "--force",
  ]);
  assert.deepEqual(wgsCommand("fastq", "multiqc-directory", {
    multiqc_directory: "directory with spaces", out_dir: "new reports",
  }), [
    "qc", "multiqc", "--input", "directory with spaces", "--outdir", "new reports",
  ]);
});
