"""An agent that knows only `wgsextract --help` finds and runs the command that
makes a 23andMe-style file.

Each step reads only what the previous command printed: the top-level help
points to the JSON index, the index names the command and its global options,
`help <command> --json` gives an example, and a mistyped flag is corrected from
the usage error. Nothing here names microarray or its flags.
"""

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from tests.htslib_helpers import needs_htslib
from tests.smoke_utils import state_directory_setting
from tests.test_microarray_library import MARKERS, _library, _sample_bam
from wgsextract_cli.core.dependencies import get_tool_path

pytestmark = needs_htslib


def _cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "wgsextract_cli.main", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _quoted_commands(text: str) -> list[str]:
    return re.findall(r"'(wgsextract [^']+)'", " ".join(text.split()))


def _flag(options: list[dict], dest: str) -> str:
    return next(o["flags"][0] for o in options if o["dest"] == dest)


def test_an_agent_gets_from_top_level_help_to_a_23andme_file(tmp_path: Path):
    library = tmp_path / "reference"
    fasta, seq = _library(library)
    templates = library / "microarray" / "raw_file_templates"
    # The examples ask for the current 23andMe chip, whose body has two parts.
    shutil.copy(
        templates / "head" / "23andMe_V3.txt", templates / "head" / "23andMe_V5.txt"
    )
    shutil.copy(
        templates / "body" / "23andMe_V3.txt", templates / "body" / "23andMe_V5_1.txt"
    )
    (templates / "body" / "23andMe_V5_2.txt").write_text("")
    bam = _sample_bam(tmp_path, seq)
    config = tmp_path / "config.toml"
    config.write_text(
        f'reference_library = "{library.as_posix()}"\n'
        + state_directory_setting(tmp_path)
    )

    # 1. The top-level help names the machine-readable index.
    top = _cli("--help").stdout
    index_command = next(c for c in _quoted_commands(top) if "--json" in c)
    index = json.loads(_cli(*shlex.split(index_command)[1:]).stdout)

    # 2. The index finds the command whose outputs are 23andMe files.
    command = next(
        c for c in index["commands"] if "23andme" in (c["outputs"] or "").lower()
    )
    assert command["runtime"] in {"seconds", "minutes"}
    assert all(get_tool_path(tool) for tool in command["tools"])

    # 3. Its own help gives an example to adapt.
    detail = json.loads(_cli("help", *command["path"].split(), "--json").stdout)
    example = next(
        e for e in detail["commands"][0]["examples"] if "23andme" in e.lower()
    )
    words = shlex.split(example)[1:]
    globals_ = index["global_options"]
    input_flag = _flag(globals_, "input")
    words[words.index(input_flag) + 1] = bam
    outdir = tmp_path / "out"
    words += [
        _flag(globals_, "ref"),
        str(fasta),
        _flag(globals_, "outdir"),
        str(outdir),
    ]
    words += [_flag(globals_, "config_path"), str(config), _flag(globals_, "json")]

    # 4. A typo is a usage error (exit 2) that names the right flag.
    typo = [
        w if w != input_flag else input_flag[:-2] + input_flag[:-3:-1] for w in words
    ]
    mistake = _cli(*typo, check=False)
    assert mistake.returncode == 2
    assert input_flag in mistake.stderr
    assert not mistake.stdout

    # 5. The run's JSON result (stdout carries nothing else) lists the kit.
    run = _cli(*words, check=False)
    assert run.returncode == 0, run.stderr[-3000:]
    result = json.loads(run.stdout)
    assert result["status"] == "ok"
    kit = next(
        Path(o["path"])
        for o in result["outputs"]
        if "23andme" in Path(o["path"]).name.lower()
    )
    genotypes = {
        line.split("\t")[0]: line.split("\t")[3].strip()
        for line in kit.read_text().splitlines()
        if not line.startswith("#")
    }
    assert genotypes == {rsid: genotype for rsid, _, genotype in MARKERS}
