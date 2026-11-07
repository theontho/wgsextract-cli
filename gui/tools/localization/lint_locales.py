#!/usr/bin/env python3
"""Dev tool: lint GUI-for-CLI bundle localization TOML files.

Usage:
  tools/localization/lint_locales.py [--strict] [--json] [--update-source-hashes] [PATH ...]

- PATH may be a bundle directory (containing strings/strings.<code>.toml),
  a strings folder, a specific strings.<code>.toml file, or omitted to
  auto-scan resources/BuiltinStrings/ plus examples/*/strings/ in the cwd.
- Reports parse errors, missing/extra/empty/duplicate keys, missing built-in
  keys, invalid layoutDirection, untranslated values, and source drift.
"""

from __future__ import annotations

import sys

from locale_linter.cli import main


if __name__ == "__main__":
    sys.exit(main())
