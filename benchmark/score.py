"""Score an answer locally against the public-1 reference, with Atira's point weights.

    my-agent/.venv/bin/python benchmark/score.py <answer.json | resolution.json> [--reference FILE]

The reference (benchmark/reference/public-1-answer.json, git-ignored) is the answer of test run
run_d6be3092688e69a9, which scored quality 100: every field full points. The weights are the ones that run reported:
bid case 5 each for decision, reference, discount, total and arithmetic consistency; 5 each for the withdrawn Q&A
quantity and the date-selected metal index; the remaining 65 split equally over the positions, and within a
position material 20 %, quantity 15 %, base price 15 %, the other five fields 10 % each.

Two checks are approximations, since Atira doesn't say exactly what they compare: `withdrawn_answer` compares the
quantity of the position whose clarification was withdrawn (01.05 in public-1) and `index_window` requires every
surcharge to match. Never copy the reference into a package: it is the solved scenario.
"""
from __future__ import annotations

import argparse
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFERENCE = HERE / "reference" / "public-1-answer.json"
WITHDRAWN_POSITION = "01.05"   # public-1: the position whose quantity answer was withdrawn

HEADER = {"decision": 5, "tender_reference": 5, "discount_percent": 5, "total_net": 5, "total_consistency": 5}
CHECKS = {"withdrawn_answer": 5, "index_window": 5}
POSITION_SHARE = {"material_number": .20, "quantity": .15, "base_unit_price": .15, "surcharge_per_unit": .10,
                  "packaging_per_unit": .10, "rebate_total": .10, "unit_price": .10, "lead_time_weeks": .10}
MONEY = {"discount_percent", "total_net", "unit_price", "base_unit_price", "surcharge_per_unit",
         "packaging_per_unit", "rebate_total"}


def same(field: str, got, want) -> bool:
    if got is None or want is None:
        return got is want
    if field in MONEY:
        return abs(Decimal(str(got)) - Decimal(str(want))) < Decimal("0.005")
    if field in ("quantity", "lead_time_weeks"):
        return int(got) == int(want)
    return str(got).strip() == str(want).strip()


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def score(answer: dict, reference: dict) -> tuple[float, list[tuple[str, float, float, object, object]]]:
    """Total points and one row per field: (field, points, max, entered, expected)."""
    rows = []
    for field, points in HEADER.items():
        if field == "total_consistency":
            positions = answer.get("positions") or []
            try:
                expected = cents(sum((Decimal(str(p["quantity"])) * Decimal(str(p["unit_price"]))
                                      for p in positions), Decimal(0)))
                ok = same("total_net", answer.get("total_net"), expected)
            except (KeyError, TypeError, ValueError):
                expected, ok = None, False
            rows.append((field, points if ok else 0, points, answer.get("total_net"), expected))
            continue
        ok = same(field, answer.get(field), reference.get(field))
        rows.append((field, points if ok else 0, points, answer.get(field), reference.get(field)))

    if reference["decision"] == "bid":
        ours = {p["position"]: p for p in answer.get("positions") or []}
        theirs = {p["position"]: p for p in reference["positions"]}
        per_position = (100 - sum(HEADER.values()) - sum(CHECKS.values())) / len(theirs)
        for position, want in theirs.items():
            got = ours.get(position, {})
            for field, share in POSITION_SHARE.items():
                points = round(per_position * share, 4)
                ok = field in got and same(field, got.get(field), want.get(field))
                rows.append((f"{position}.{field}", points if ok else 0, points, got.get(field), want.get(field)))
        got = ours.get(WITHDRAWN_POSITION, {}).get("quantity")
        want = theirs[WITHDRAWN_POSITION]["quantity"]
        rows.append(("withdrawn_answer", 5 if same("quantity", got, want) else 0, 5, got, want))
        ok = all(same("surcharge_per_unit", ours.get(p, {}).get("surcharge_per_unit"), w["surcharge_per_unit"])
                 for p, w in theirs.items())
        rows.append(("index_window", 5 if ok else 0, 5, "all surcharges" if ok else "a surcharge differs", ""))
        extra = sorted(set(ours) - set(theirs))
        if extra:
            rows.append(("extra_positions", 0, 0, extra, []))
    return round(sum(r[1] for r in rows), 2), rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("answer", type=Path)
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    parser.add_argument("--all", action="store_true", help="also list the fields that are right")
    args = parser.parse_args()
    data = json.loads(args.answer.read_text())
    answer = data.get("answer", data) if isinstance(data, dict) else data
    total, rows = score(answer, json.loads(args.reference.read_text()))
    for field, points, maximum, got, want in rows:
        if args.all or points < maximum:
            mark = "ok " if points >= maximum else "XX "
            print(f"{mark}{field:36} {points:5.2f}/{maximum:<5.2f} entered {got!r}  expected {want!r}")
    print(f"quality {total} / 100")


if __name__ == "__main__":
    main()
