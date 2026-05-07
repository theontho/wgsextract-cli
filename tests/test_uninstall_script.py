from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL_SH = ROOT / "uninstall.sh"


def make_fake_install(root: Path) -> Path:
    install_dir = root / "wgsextract-cli"
    app_dir = install_dir / "app"
    pixi_dir = install_dir / ".pixi"
    app_dir.mkdir(parents=True)
    (app_dir / "pixi.toml").write_text("[workspace]\nname = 'fake'\n", encoding="utf-8")
    (pixi_dir / "cache").mkdir(parents=True)
    (pixi_dir / "envs").mkdir(parents=True)
    (install_dir / "wgsextract").write_text("#!/bin/sh\n", encoding="utf-8")
    shutil.copy(UNINSTALL_SH, install_dir / "uninstall.sh")
    return install_dir


def run_uninstall(
    script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["/bin/sh", str(script), *args],
        check=True,
        env=run_env,
        text=True,
        capture_output=True,
    )


def test_uninstall_removes_default_install_tree(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)

    result = run_uninstall(install_dir / "uninstall.sh", "--yes")

    assert "Uninstall complete." in result.stdout
    assert not install_dir.exists()


def test_uninstall_dry_run_keeps_install_tree(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)

    result = run_uninstall(install_dir / "uninstall.sh", "--dry-run")

    assert "Dry run only" in result.stdout
    assert install_dir.exists()


def test_uninstall_removes_external_paths_when_configured(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)
    bin_dir = tmp_path / "bin"
    cache_dir = tmp_path / "cache"
    env_dir = tmp_path / "envs"
    bin_dir.mkdir()
    cache_dir.mkdir()
    env_dir.mkdir()
    (bin_dir / "wgsextract").write_text(
        '#!/bin/sh\nexec /path/to/pixi run wgsextract "$@"\n',
        encoding="utf-8",
    )

    run_uninstall(
        install_dir / "uninstall.sh",
        "--yes",
        env={
            "WGSEXTRACT_BIN_DIR": str(bin_dir),
            "WGSEXTRACT_PIXI_CACHE_DIR": str(cache_dir),
            "WGSEXTRACT_PIXI_ENV_DIR": str(env_dir),
        },
    )

    assert not install_dir.exists()
    assert not (bin_dir / "wgsextract").exists()
    assert not cache_dir.exists()
    assert not env_dir.exists()


def test_uninstall_refuses_external_launcher_that_is_not_generated(
    tmp_path: Path,
) -> None:
    install_dir = make_fake_install(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "wgsextract"
    launcher.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")

    result = subprocess.run(
        ["/bin/sh", str(install_dir / "uninstall.sh"), "--yes"],
        env={
            **os.environ,
            "WGSEXTRACT_BIN_DIR": str(bin_dir),
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "does not look like WGS Extract CLI" in result.stderr
    assert launcher.exists()
    assert install_dir.exists()


def test_uninstall_refuses_unsafe_external_directory(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)

    result = subprocess.run(
        ["/bin/sh", str(install_dir / "uninstall.sh"), "--yes"],
        env={
            **os.environ,
            "WGSEXTRACT_PIXI_CACHE_DIR": "/",
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Refusing to remove unsafe directory" in result.stderr
    assert install_dir.exists()


def test_uninstall_refuses_normalized_unsafe_external_directory(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)

    result = subprocess.run(
        ["/bin/sh", str(install_dir / "uninstall.sh"), "--yes"],
        env={
            **os.environ,
            "WGSEXTRACT_PIXI_CACHE_DIR": "/usr/../usr",
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Refusing to remove unsafe directory" in result.stderr
    assert install_dir.exists()


def test_uninstall_refuses_symlinked_external_directory(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)
    victim_dir = tmp_path / "victim"
    cache_dir = victim_dir / "cache"
    link_dir = tmp_path / "link"
    cache_dir.mkdir(parents=True)
    (cache_dir / "keep.txt").write_text("do not delete\n", encoding="utf-8")
    link_dir.symlink_to(victim_dir, target_is_directory=True)

    result = subprocess.run(
        ["/bin/sh", str(install_dir / "uninstall.sh"), "--yes"],
        env={
            **os.environ,
            "WGSEXTRACT_PIXI_CACHE_DIR": str(link_dir / "cache"),
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Refusing to remove directory through a symlink" in result.stderr
    assert (cache_dir / "keep.txt").exists()
    assert install_dir.exists()
