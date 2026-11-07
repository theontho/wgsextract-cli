from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "site_src"
OUTPUT_DIR = ROOT / "site"
BASE_URL = "https://wgsextract.github.io/wgsextract-cli/app"
GITHUB_URL = "https://github.com/WGSExtract/wgsextract-cli"
SOCIAL_IMAGE_URL = f"{BASE_URL}/assets/social-preview.webp"
SOCIAL_IMAGE_ALT = "WGSExtract social preview showing the desktop app screenshot."
SOCIAL_IMAGE_WIDTH = 1200
SOCIAL_IMAGE_HEIGHT = 630

NAV_ITEMS = (
    ("index.html", "Home"),
    ("architecture.html", "Architecture"),
    ("wgsextract.html", "WGSExtract"),
    ("docs.html", "Docs"),
)


@dataclass(frozen=True)
class Page:
    source: Path
    output_name: str
    title: str
    description: str
    eyebrow: str
    heading: str
    lede: str
    footer_title: str
    footer_text: str
    body: str
    actions: tuple[tuple[str, str, str], ...]
    auto_hero: bool
