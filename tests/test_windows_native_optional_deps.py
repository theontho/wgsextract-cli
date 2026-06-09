from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pacman_setup_declares_native_optional_packages() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert "$optionalRuntimePackages = @(" in script
    assert "mingw-w64-ucrt-x86_64-curl" in script
    assert "htsfile is provided by mingw-w64-ucrt-x86_64-htslib" in script


def test_pacman_setup_reports_native_optional_tools() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert "$optionalPacmanTools = @(" in script
    assert '"curl"' in script
    assert '"htsfile"' in script
    assert '"minimap2"' in script
    assert "Optional pacman runtime tools are present:" in script


def test_pacman_setup_can_build_and_install_minimap2_asset() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert '[string]$Minimap2Version = "2.30"' in script
    assert '[string]$Minimap2BinaryUrl = ""' in script
    assert '[string]$Minimap2BinarySha256 = ""' in script
    assert "WGSEXTRACT_MINIMAP2_BINARY_URL" not in script
    assert "WGSEXTRACT_MINIMAP2_BINARY_SHA256" not in script
    assert 'Assert-SafeReleaseValue -Name "Minimap2Version"' in script
    assert '-Sha256ParameterName "Minimap2BinarySha256"' in script
    assert "Install-Minimap2BinaryPackage" in script
    assert "wgsextract-minimap2-$Minimap2Version-windows-ucrt64.zip" in script
    assert "Building minimap2 $Minimap2Version for MSYS2 UCRT64" in script
    assert "/ucrt64/bin/minimap2.exe --version" in script
    assert 'throw "Failed to build required minimap2 native runtime:' in script


def test_release_workflow_uploads_minimap2_asset() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release_windows_bwa.yml").read_text(
        encoding="utf-8"
    )

    assert "MINIMAP2_VERSION" in workflow
    assert "Resolve-SafeBuildInput" in workflow
    assert "INPUT_MINIMAP2_VERSION: ${{ inputs.minimap2_version }}" in workflow
    assert '$minimap2Version = "${{ inputs.minimap2_version }}"' not in workflow
    assert "Build native tools with MSYS2 UCRT64 GCC" in workflow
    assert "wgsextract-minimap2-$env:MINIMAP2_VERSION-windows-ucrt64" in workflow
    assert "minimap2.exe" in workflow
    assert "${{ env.MINIMAP2_ZIP }}" in workflow


def test_windows_installer_exposes_minimap2_checksum_passthrough() -> None:
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")

    assert "--minimap2-binary-url" in installer
    assert "--minimap2-binary-sha256" in installer
    assert "-Minimap2BinarySha256" in installer
