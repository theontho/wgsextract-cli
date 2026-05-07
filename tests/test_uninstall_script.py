from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL_SH = ROOT / "uninstall.sh"
SH = shutil.which("sh") or "sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None,
    reason="POSIX sh is required to test uninstall.sh",
)


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
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        [SH, str(script), *args],
        check=True,
        env=run_env,
        text=True,
        capture_output=True,
        input=input_text,
    )


def make_fake_pixi_home(home: Path) -> Path:
    pixi_home = home / ".pixi"
    pixi_bin = pixi_home / "bin"
    pixi_bin.mkdir(parents=True)
    pixi = pixi_bin / "pixi"
    pixi.write_text("#!/bin/sh\necho pixi 0.0.0\n", encoding="utf-8")
    pixi.chmod(0o755)
    (home / ".zshrc").write_text(
        'export OTHER=1\nexport PATH="$HOME/.pixi/bin:$PATH"\n',
        encoding="utf-8",
    )
    fish_config = home / ".config" / "fish" / "config.fish"
    fish_config.parent.mkdir(parents=True)
    fish_config.write_text(
        "set -x PIXI_BIN_PATH $HOME/.pixi/bin\nset -gx PATH $HOME/.pixi/bin $PATH\n",
        encoding="utf-8",
    )
    return pixi_home


def assert_profile_backup_exists(home: Path) -> None:
    assert list(home.glob(".zshrc.wgsextract-uninstall-backup.*"))


def test_uninstall_removes_default_install_tree(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)

    result = run_uninstall(install_dir / "uninstall.sh", "--yes")

    assert "Uninstall complete." in result.stdout
    assert not install_dir.exists()


def test_uninstall_yes_keeps_pixi_by_default(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)
    home = tmp_path / "home"
    pixi_home = make_fake_pixi_home(home)

    result = run_uninstall(
        install_dir / "uninstall.sh",
        "--yes",
        env={"HOME": str(home)},
    )

    assert "Pass --remove-pixi" in result.stdout
    assert not install_dir.exists()
    assert (pixi_home / "bin" / "pixi").exists()
    assert ".pixi/bin" in (home / ".zshrc").read_text(encoding="utf-8")


def test_uninstall_eof_cancels_without_error(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)

    result = subprocess.run(
        [SH, str(install_dir / "uninstall.sh")],
        env=os.environ,
        text=True,
        capture_output=True,
        input="",
    )

    assert result.returncode == 0
    assert "Uninstall cancelled." in result.stdout
    assert install_dir.exists()


def test_uninstall_interactive_can_remove_pixi(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)
    home = tmp_path / "home"
    pixi_home = make_fake_pixi_home(home)

    result = run_uninstall(
        install_dir / "uninstall.sh",
        env={"HOME": str(home)},
        input_text="y\ny\n",
    )

    assert "Remove Pixi too?" in result.stdout
    assert not install_dir.exists()
    assert not pixi_home.exists()
    zshrc = (home / ".zshrc").read_text(encoding="utf-8")
    assert "export OTHER=1" in zshrc
    assert 'export PATH="$HOME/.pixi/bin:$PATH"' not in zshrc
    fish_config = (home / ".config" / "fish" / "config.fish").read_text(
        encoding="utf-8"
    )
    assert "set -x PIXI_BIN_PATH $HOME/.pixi/bin" in fish_config
    assert "set -gx PATH $HOME/.pixi/bin $PATH" not in fish_config
    assert_profile_backup_exists(home)


def test_uninstall_yes_remove_pixi_removes_pixi(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)
    home = tmp_path / "home"
    pixi_home = make_fake_pixi_home(home)

    result = run_uninstall(
        install_dir / "uninstall.sh",
        "--yes",
        "--remove-pixi",
        env={"HOME": str(home)},
    )

    assert "Removing Pixi directory" in result.stdout
    assert not install_dir.exists()
    assert not pixi_home.exists()
    assert 'export PATH="$HOME/.pixi/bin:$PATH"' not in (home / ".zshrc").read_text(
        encoding="utf-8"
    )
    assert_profile_backup_exists(home)


def test_uninstall_pixi_profile_cleanup_preserves_non_path_mentions(
    tmp_path: Path,
) -> None:
    install_dir = make_fake_install(tmp_path)
    home = tmp_path / "home"
    pixi_home = make_fake_pixi_home(home)
    (home / ".zshrc").write_text(
        "\n".join(
            [
                "# TODO: check whether ~/.pixi/bin is needed later",
                'alias pixi-note="echo ~/.pixi/bin/pixi"',
                'export PATH="/usr/bin:$PATH"  # TODO: maybe add ~/.pixi/bin later',
                'export PATH="$HOME/.pixi/bin:$PATH"',
                "export OTHER=1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    run_uninstall(
        install_dir / "uninstall.sh",
        "--yes",
        "--remove-pixi",
        env={"HOME": str(home)},
    )

    zshrc = (home / ".zshrc").read_text(encoding="utf-8")
    assert not pixi_home.exists()
    assert "# TODO: check whether ~/.pixi/bin is needed later" in zshrc
    assert 'alias pixi-note="echo ~/.pixi/bin/pixi"' in zshrc
    assert 'export PATH="/usr/bin:$PATH"  # TODO: maybe add ~/.pixi/bin later' in zshrc
    assert 'export PATH="$HOME/.pixi/bin:$PATH"' not in zshrc
    assert "export OTHER=1" in zshrc
    assert_profile_backup_exists(home)


def test_uninstall_remove_pixi_handles_missing_pixi_executable(
    tmp_path: Path,
) -> None:
    install_dir = make_fake_install(tmp_path)
    home = tmp_path / "home"
    pixi_home = make_fake_pixi_home(home)
    (pixi_home / "bin" / "pixi").unlink()

    result = run_uninstall(
        install_dir / "uninstall.sh",
        "--yes",
        "--remove-pixi",
        env={"HOME": str(home)},
    )

    assert "Removing Pixi directory" in result.stdout
    assert not install_dir.exists()
    assert not pixi_home.exists()


def test_uninstall_remove_pixi_refuses_symlinked_shell_profile(
    tmp_path: Path,
) -> None:
    install_dir = make_fake_install(tmp_path)
    home = tmp_path / "home"
    pixi_home = make_fake_pixi_home(home)
    dotfiles_dir = home / "dotfiles"
    dotfiles_dir.mkdir()
    zshrc_target = dotfiles_dir / "zshrc"
    zshrc_target.write_text(
        'export PATH="$HOME/.pixi/bin:$PATH"\n',
        encoding="utf-8",
    )
    (home / ".zshrc").unlink()
    (home / ".zshrc").symlink_to(zshrc_target)

    result = subprocess.run(
        [SH, str(install_dir / "uninstall.sh"), "--yes", "--remove-pixi"],
        env={
            **os.environ,
            "HOME": str(home),
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Refusing to edit symlinked shell profile" in result.stderr
    assert (home / ".zshrc").is_symlink()
    assert pixi_home.exists()


def test_uninstall_refuses_empty_home(tmp_path: Path) -> None:
    install_dir = make_fake_install(tmp_path)

    result = subprocess.run(
        [SH, str(install_dir / "uninstall.sh"), "--yes"],
        env={
            **os.environ,
            "HOME": "",
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "HOME environment variable is not set or is unsafe" in result.stderr
    assert install_dir.exists()


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
        '#!/bin/sh\n# WGS Extract CLI installer launcher\nexec /path/to/pixi run wgsextract "$@"\n',
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
        [SH, str(install_dir / "uninstall.sh"), "--yes"],
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
        [SH, str(install_dir / "uninstall.sh"), "--yes"],
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
        [SH, str(install_dir / "uninstall.sh"), "--yes"],
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
        [SH, str(install_dir / "uninstall.sh"), "--yes"],
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
