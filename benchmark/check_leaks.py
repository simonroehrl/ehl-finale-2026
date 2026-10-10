"""Fail if a package folder contains values from the solved public scenario (the reference answer).

    my-agent/.venv/bin/python benchmark/check_leaks.py cc-agent

Checks every file the package would include (git-ignored files are skipped) for the reference's tender reference,
material numbers and money values, in plain and German number format.
"""
import json
import subprocess
import sys
from pathlib import Path

REFERENCE = Path(__file__).resolve().parent / "reference" / "public-1-answer.json"


def needles(answer: dict) -> set[str]:
    found = {answer["tender_reference"]}
    for p in answer["positions"]:
        found.add(p["material_number"])
        for field in ("unit_price", "base_unit_price", "rebate_total"):
            value = f"{p[field]:.2f}"
            if p[field] >= 100:  # small numbers are too common to mean anything
                found |= {value, value.replace(".", ",")}
    total = f"{answer['total_net']:.2f}"
    return found | {total, total.replace(".", ",")}


def main(package: Path) -> int:
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "."], cwd=package,
                           capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for name in files:
        try:
            text = (package / name).read_text(errors="ignore")
        except (OSError, IsADirectoryError):
            continue
        hits += [(name, n) for n in needles(json.loads(REFERENCE.read_text())) if n in text]
    for name, needle in hits:
        print(f"LEAK {name}: {needle}")
    print(f"{len(files)} files checked, {len(hits)} leaks")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]).resolve()))
