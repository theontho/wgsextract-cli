"""Concept pages for 'wgsextract help <topic>'.

Each body is plain text wrapped at 78 columns. Indented lines that start with
"wgsextract " are runnable examples; tests/test_cli_help.py checks that they
parse and that no topic shares a name with a command.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class HelpTopic:
    summary: str
    body: str


HELP_TOPICS: dict[str, HelpTopic] = {
    "references": HelpTopic(
        "Reference genomes: what they are, where they come from.",
        """\
A reference genome is the sequence your reads were mapped against. Most
commands need the one your BAM/CRAM was made with, and a CRAM can't be read
without it. Its version is the build ('wgsextract help builds').

You rarely need to name it. --ref takes a FASTA file or a folder; without
--ref, the 'ref' setting in config.toml is used, then the reference library
(the 'reflib' setting). WGS Extract picks the FASTA whose sequence dictionary
matches the BAM/CRAM header, so a library can hold several builds.

Set up the reference library once, then check a FASTA if a command complains:
  wgsextract ref library --list
  wgsextract ref library --install hs38
  wgsextract ref bootstrap
  wgsextract ref verify --ref hs38.fa
""",
    ),
    "builds": HelpTopic(
        "Genome builds (hg19, hg38, T2T) and moving between them.",
        """\
A build is a version of the human reference genome. The same position has
different coordinates in different builds, so a BAM/CRAM, its reference and
any VCF or annotation used with it must all be the same build.
  hg19 / GRCh37   older; many older BAMs and consumer tests use it
  hg38 / GRCh38   the current standard for most labs
  T2T / CHM13     the first complete, gap-free human genome

'info' reports a file's build. To move a BAM/CRAM to another build, re-map
its reads with 'realign' (--ref names the build to move to):
  wgsextract info --input sample.cram
  wgsextract realign --input sample_hg19.bam --ref hs38.fa
""",
    ),
    "genome-library": HelpTopic(
        "Keep each person's files in a folder; select it with --genome.",
        """\
Set 'genome_library' in config.toml to a folder with one subfolder per
person or sample. The subfolder name (or a nested path such as
'family/joe') is the genome ID. With --genome ID, commands find their
inputs in that folder and write their outputs there unless --outdir is given.
If the folder holds several BAM/CRAM files, VCFs or FASTQ sets, the command
stops rather than guessing: name the files to use in the genome-config.toml
it creates in the folder.
  wgsextract info --genome joe
  wgsextract microarray --genome joe --formats 23andme_v5

'example-genome download' puts public practice genomes in the library.
""",
    ),
    "coverage": HelpTopic(
        "Coverage and depth, and the low-depth warnings.",
        """\
Depth is how many reads cover a position: "30x" means 30 reads per base on
average. Breadth is the share of the genome covered at all. Low depth means
more missing and wrong genotypes, so commands warn below 8x on autosomes and
3x on chrY and mtDNA (the qc_min_diploid_depth and qc_min_haploid_depth
settings; --qc-min-depth for one run).
  wgsextract info coverage-sample --input sample.cram
  wgsextract info calculate-coverage --input sample.cram

coverage-sample is a quick estimate; calculate-coverage reads the whole file.
""",
    ),
    "exit-codes": HelpTopic(
        "What wgsextract's exit codes mean.",
        """\
  0       success
  1       the command failed; the error says why
  2       usage error: an unknown command or option, or a bad value; nothing
          ran ('Did you mean ...?' suggests a fix)
  3       a required external tool is missing ('wgsextract deps check')
  128+N   stopped by signal N (130 is Ctrl-C)

With --json, the result document's exit_code and error.kind say the same
('wgsextract help manifests').
""",
    ),
    "manifests": HelpTopic(
        "The result manifest each run writes, and --json.",
        """\
A command that writes files also writes <output>.manifest.json beside them:
the inputs and outputs (path, size, fingerprint), headline metrics such as
the haplogroup or depth, warnings, the command line and tool versions. Its
schema is wgsextract.result/1. Read it instead of parsing log text or
guessing file names.

--json prints the same document on stdout instead of the human-readable
result, for every command; logs stay on stderr.
  wgsextract info --input sample.cram --json

docs/result-manifest.md describes every field and warning code.
""",
    ),
    "privacy": HelpTopic(
        "Your genome is identifying: what stays local and sharing.",
        """\
Whole-genome data identifies you and your relatives. WGS Extract runs on
your computer and uploads nothing; only downloads (references, annotation
data, example genomes) use the network. Even small derived files, such as a
23andMe-style file or a filtered VCF, identify you, and logs can name your
files and samples.

Back up the original BAM/CRAM/FASTQ files and record checksums. Before
sharing, prefer the smallest file that answers the question, and check what
the service does with it. 'report-issue' drafts a bug report with personal
details removed, for you to review before sending.
  wgsextract sign create results/
""",
    ),
    "scripting": HelpTopic(
        "Use wgsextract from scripts and AI agents.",
        """\
'help --all --json' lists every command with its options, examples and
outputs (schema wgsextract.help/1); 'help <command> --json' gives one. The
commands of the typed Python API also carry a JSON Schema of their
parameters ("input_schema"; "x-cli-flag" names each flag). "tools" are the
external tools a command checks for ('wgsextract deps check' shows which
are installed) and "runtime" is instant, seconds, minutes, hours or
download (on a 30x genome unless "runtime_text" says otherwise). New fields may
appear under the same schema, so ignore keys you don't know; a renamed or
removed field gets a new schema version.
  wgsextract help --all --json
  wgsextract help microarray --json

--json on any command prints only the result document on stdout
('wgsextract help manifests'); logs go to stderr. Without a terminal, or
with NO_COLOR, output has no colour, and 'Next:' hints appear only on a
terminal. --quiet hides informational logs. Exit codes are stable
('wgsextract help exit-codes'). A mistyped command or option exits 2 with a
suggestion. Python callers can use the typed API (docs/api.md).
""",
    ),
}
