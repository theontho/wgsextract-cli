# Install dependencies via Conda (PowerShell)

param (
    [switch]$SkipConda
)

$ErrorActionPreference = "Stop"

if (-not $SkipConda) {
    if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
        echo "Conda not found. Please install Miniconda manually on Windows or use WSL2."
        # Automatic silent install on Windows is more complex, pointing user to manual install for now.
        exit 1
    }
}

echo "Updating Conda environments and installing tools..."
conda install -y -c bioconda -c conda-forge samtools bcftools htslib bwa minimap2 fastp fastqc delly freebayes ensembl-vep openjdk python

echo "Installation complete."

echo ""
echo "⚠️  Warning: Native Windows Conda environments often lack core bioinformatics tools"
echo "   (like 'bwa', 'samtools', or 'tabix') which are required for many features."
echo "   If you encounter missing tool errors, we HIGHLY recommend using the WSL2 setup:"
echo ""
echo "   powershell ./bootstrap_wsl.ps1"
echo ""
