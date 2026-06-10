from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pacman_setup_declares_native_optional_packages() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert "$optionalRuntimePackages = @(" in script
    assert "mingw-w64-ucrt-x86_64-curl" in script
    assert "mingw-w64-ucrt-x86_64-gcc-libs" in script
    assert "mingw-w64-ucrt-x86_64-isa-l" in script
    assert "mingw-w64-ucrt-x86_64-libdeflate" in script
    assert "htsfile is provided by mingw-w64-ucrt-x86_64-htslib" in script


def test_pacman_setup_reports_native_optional_tools() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert "$optionalPacmanTools = @(" in script
    assert '"curl"' in script
    assert '"htsfile"' in script
    assert '"minimap2"' in script
    assert '"samblaster"' in script
    assert '"fastp"' in script
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


def test_pacman_setup_can_build_and_install_samblaster_and_fastp_assets() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert '[string]$SamblasterVersion = "0.1.26"' in script
    assert '[string]$FastpVersion = "0.24.1"' in script
    assert "Install-SamblasterBinaryPackage" in script
    assert "Install-FastpBinaryPackage" in script
    assert "wgsextract-samblaster-$SamblasterVersion-windows-ucrt64.zip" in script
    assert "wgsextract-fastp-$FastpVersion-windows-ucrt64.zip" in script
    assert "Building samblaster $SamblasterVersion for MSYS2 UCRT64" in script
    assert "Building fastp $FastpVersion for MSYS2 UCRT64" in script
    assert "/ucrt64/bin/samblaster.exe --version" in script
    assert "/ucrt64/bin/fastp.exe --version" in script
    assert 'throw "Failed to build required samblaster native runtime:' in script
    assert 'throw "Failed to build required fastp native runtime:' in script


def test_release_workflow_uploads_samblaster_and_fastp_assets() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release_windows_bwa.yml").read_text(
        encoding="utf-8"
    )

    assert "SAMBLASTER_VERSION" in workflow
    assert "FASTP_VERSION" in workflow
    assert "INPUT_SAMBLASTER_VERSION: ${{ inputs.samblaster_version }}" in workflow
    assert "INPUT_FASTP_VERSION: ${{ inputs.fastp_version }}" in workflow
    assert "wgsextract-samblaster-$env:SAMBLASTER_VERSION-windows-ucrt64" in workflow
    assert "wgsextract-fastp-$env:FASTP_VERSION-windows-ucrt64" in workflow
    assert "samblaster.exe" in workflow
    assert "fastp.exe" in workflow
    assert "${{ env.SAMBLASTER_ZIP }}" in workflow
    assert "${{ env.FASTP_ZIP }}" in workflow


def test_windows_installer_exposes_minimap2_checksum_passthrough() -> None:
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")

    assert "--minimap2-binary-url" in installer
    assert "--minimap2-binary-sha256" in installer
    assert "-Minimap2BinarySha256" in installer


def test_windows_installer_exposes_samblaster_fastp_checksum_passthrough() -> None:
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")

    assert "--samblaster-binary-url" in installer
    assert "--samblaster-binary-sha256" in installer
    assert "-SamblasterBinarySha256" in installer
    assert "--fastp-binary-url" in installer
    assert "--fastp-binary-sha256" in installer
    assert "-FastpBinarySha256" in installer


def test_windows_installer_persists_hybrid_runtime() -> None:
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")

    assert "'tool_runtime':'windows'" in installer
    assert "Runtime defaults were set to windows" in installer


def test_pixi_declares_native_windows_optional_tools() -> None:
    pixi = (ROOT / "pixi.toml").read_text(encoding="utf-8")

    assert "[feature.gatk.target.win-64.dependencies]" in pixi
    assert 'gatk4 = ">=4.5.0.0"' in pixi
    assert "[feature.bio-tools.target.win-64.dependencies]" in pixi
    assert 'fastqc = ">=0.12.1"' in pixi
    assert 'haplogrep = ">=2.4.0"' in pixi
    assert 'yleaf = ">=2.2"' in pixi
