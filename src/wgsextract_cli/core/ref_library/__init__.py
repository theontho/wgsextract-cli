"""Reference library package: genome catalog (`catalog`) and downloads (`downloads`)."""

from .catalog import get_available_genomes
from .downloads import download_file

__all__ = ["download_file", "get_available_genomes"]
