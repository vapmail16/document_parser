"""Licence gate (spec §5): fail on copyleft or unknown licences in the installed environment.

Usage:  uv run python scripts/check_licences.py
Exceptions go in scripts/licence_exceptions.txt (one package name per line, with a comment
saying who approved it and why).
"""

import json
import subprocess
import sys
from pathlib import Path

ALLOWED_MARKERS = (
    "MIT", "BSD", "APACHE", "ISC", "PSF", "PYTHON SOFTWARE FOUNDATION", "MOZILLA PUBLIC LICENSE 2.0",
    "MPL-2.0", "MPL 2.0", "HPND", "UNLICENSE", "0BSD", "ZLIB", "CC0",
)
DENIED_MARKERS = ("GPL", "AGPL", "LGPL", "SSPL", "COMMONS CLAUSE", "BUSL", "BUSINESS SOURCE")
EXCEPTIONS_FILE = Path(__file__).with_name("licence_exceptions.txt")


def classify(licence: str) -> str:
    upper = licence.upper()
    if any(m in upper for m in DENIED_MARKERS):
        return "denied"
    if any(m in upper for m in ALLOWED_MARKERS):
        return "allowed"
    return "unknown"


def load_exceptions() -> set[str]:
    if not EXCEPTIONS_FILE.exists():
        return set()
    lines = (ln.split("#", 1)[0].strip().lower() for ln in EXCEPTIONS_FILE.read_text().splitlines())
    return {ln for ln in lines if ln}


def main() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-system", "--from=mixed",
         "--ignore-packages", "credit-extract"],  # this project itself: internal, no public licence
        check=True, capture_output=True, text=True,
    ).stdout
    exceptions = load_exceptions()
    failures = []
    for pkg in json.loads(out):
        name, licence = pkg["Name"], pkg["License"]
        verdict = classify(licence)
        if verdict != "allowed" and name.lower() not in exceptions:
            failures.append(f"  {verdict:<8} {name}=={pkg['Version']}  [{licence}]")
    if failures:
        print("Licence gate FAILED (spec §5):\n" + "\n".join(failures))
        return 1
    print("Licence gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
