"""Bid rules. Pure functions: evidence in, one answer out. No model and no browser.

The arithmetic follows the challenge brief. Money stays in Decimal and is rounded HALF_UP to the
cent only where the brief says so. Reported breakdown fields are not fed back into the final price.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from records import (
    Evidence,
    Material,
    Message,
    Position,
    QA,
    Requirement,
    Revision,
    clean,
    ref_key,
)

CENT = Decimal("0.01")
ZERO = Decimal("0")
SCOPE_BASE = "base"
SCOPE_BOTH = "base_and_surcharge"
SCOPE_NONE = "none"
LARGEST_REMAINDER = "floor_largest_remainder_ascending_position"
ATTRIBUTES = ("bore_mm", "stroke_mm", "pressure_bar", "mounting", "certification")
NO_CERTIFICATION = {"", "none", "keine", "keine gefordert", "nicht gefordert", "ohne", "-", "n/a", "—", "–"}
DEFAULT_SENDERS = {
    "discount_approval": "leitung",
    "condition_rules": "kalkulation",
    "metal_index": "kalkulation",
    "setup_pools": "kalkulation",
    "technical_release": "technik",
    "packaging_tariffs": "logistik",
    "delivery_commitments": "logistik",
    "fx_fixing": "finanzen",
    "project_bonus": "finanzen",
}


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def cents(value: Decimal) -> Decimal:
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> float:
    return float(cents(value))


def position_key(position: str):
    return tuple(int(part) if part.isdigit() else part for part in position.replace("-", ".").split("."))


def address_of(sender: str) -> str:
    found = re.search(r"[\w.+-]+@[\w.-]+", sender or "")
    return found.group(0).lower() if found else ""


def certification(value: str | None) -> str:
    text = clean(value).casefold()
    return "none" if text in NO_CERTIFICATION else text.replace(" ", "")


def same(attribute: str, left, right) -> bool:
    if attribute == "mounting":
        return clean(left).casefold() == clean(right).casefold()
    if attribute == "certification":
        return certification(left) == certification(right)
    return D(left) == D(right)


def on_time(moment: datetime, scenario: datetime, date_only: bool) -> bool:
    if date_only:
        return moment.date() <= scenario.date()
    return moment <= scenario


@dataclass
class Gap:
    site: str
    question: str
    position: str | None = None


@dataclass
class Resolution:
    answer: dict | None = None
    omitted: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Missing(Exception):
    def __init__(self, site: str, question: str):
        super().__init__(question)
        self.gap = Gap(site, question)


class NoMaterial(Exception):
    pass


@dataclass
class Priced:
    position: str
    quantity: int
    material: str
    base: Decimal
    mass: Decimal
    scope: str
    setup: Decimal
    packaging: Decimal
    stock: int
    stock_weeks: int
    production_weeks: int
    commitment: tuple | None
    pool: bool
    allocation_mass: Decimal
    bonus: bool


def allocate_cents(total: Decimal, weights: dict[str, Decimal]) -> dict[str, Decimal]:
    """Split a money amount into whole cents by largest remainder. Ties go to the ascending position id."""
    total_cents = int((D(total) / CENT).to_integral_value(rounding=ROUND_HALF_UP))
    weight_sum = sum(weights.values(), ZERO)
    if total_cents == 0 or weight_sum == 0:
        return {key: ZERO for key in weights}
    exact = {key: Decimal(total_cents) * weight / weight_sum for key, weight in weights.items()}
    floors = {key: int(value) for key, value in exact.items()}
    left = total_cents - sum(floors.values())
    order = sorted(weights, key=lambda key: (-(exact[key] - floors[key]), position_key(key)))
    for key in order[:left]:
        floors[key] += 1
    return {key: Decimal(count) * CENT for key, count in floors.items()}


def project_bonus(bands: list[tuple[Decimal, Decimal | None, Decimal]], eligible_total: Decimal) -> Decimal:
    credit = ZERO
    for lower, upper, percent in bands:
        cap = eligible_total if upper is None else min(eligible_total, D(upper))
        portion = cap - D(lower)
        if portion > 0:
            credit += portion * D(percent) / 100
    return cents(credit)


def no_bid(reference: str) -> dict:
    return {
        "tender_reference": reference,
        "decision": "no_bid",
        "currency": "EUR",
        "discount_percent": 0,
        "positions": [],
        "total_net": 0,
    }


class Kernel:
    def __init__(self, evidence: Evidence, strict: bool = True):
        self.ev = evidence
        self.strict = strict
        self.trace: list[str] = []
        self.warnings: list[str] = []
        self.reference = ref_key(evidence.case.tender_reference)
        self.when = evidence.case.scenario_date
        self.day = self.when.date() if self.when else None
        self.qa = self._active_qa()

    def note(self, line: str) -> None:
        self.trace.append(line)

    def _active_qa(self) -> list[QA]:
        if self.when is None:
            return []
        dated = [
            answer for answer in self.ev.qa
            if answer.date is not None and on_time(answer.date, self.when, self.ev.case.date_only)
            and (answer.reference is None or ref_key(answer.reference) == self.reference)
        ]
        withdrawn: set[str] = set()
        while True:
            updated = set()
            for answer in dated:
                if answer.number.strip() in withdrawn:
                    continue
                updated.update(item.strip() for item in answer.withdraws if item.strip())
            if updated == withdrawn:
                break
            withdrawn = updated
        for number in sorted(withdrawn):
            self.note(f"Q&A {number} withdrawn")
        return sorted((answer for answer in dated if answer.number.strip() not in withdrawn), key=lambda answer: (answer.date, answer.number))

    def _undated_effects(self) -> list[Gap]:
        gaps = []
        for answer in self.ev.qa:
            if answer.date is not None:
                continue
            if answer.withdraws:
                gaps.append(Gap("tender-portal", f"Q&A {answer.number} withdraws another answer but has no date."))
            for change in answer.quantity_changes:
                gaps.append(Gap("tender-portal", f"Q&A {answer.number} changes a quantity but has no date.", change.position))
            for amendment in answer.amendments:
                gaps.append(Gap("tender-portal", f"Q&A {answer.number} amends {amendment.requirement_id} but has no date."))
        return gaps

    def messages(self, kind: str) -> list[Message]:
        local = (self.ev.case.authorized_senders or {}).get(kind) or DEFAULT_SENDERS[kind]
        domain = (self.ev.case.bidder_mail_domain or "").lower()
        sender = f"{local}@{domain}"
        found = [
            message for message in self.ev.messages
            if message.kind == kind and message.date is not None and self.when is not None
            and address_of(message.sender) == sender
            and ref_key(message.reference) == self.reference
            and on_time(message.date, self.when, self.ev.case.date_only)
        ]
        return sorted(found, key=lambda message: message.date)

    def latest_keyed(self, kind: str, attr: str, key: str):
        found = None
        for message in self.messages(kind):
            for entry in getattr(message, attr):
                if ref_key(entry.key) == ref_key(key):
                    found = (message, entry)
        return found

    def governing_revision(self) -> Revision:
        current = [revision for revision in self.ev.revisions if revision.marking == "current"]
        if len(current) != 1:
            found = ", ".join(revision.id for revision in current) or "none"
            raise Missing("customer-portal", f"Exactly one specification revision must be marked current (found: {found}).")
        self.note(f"specification: revision {current[0].id} is marked current")
        return current[0]

    def requirement(self, revision: Revision, position: Position) -> tuple[Requirement, dict]:
        if not position.requirement_id:
            raise Missing("tender-portal", f"Which specification requirement does position {position.position} refer to?")
        requirement = next((item for item in revision.requirements if ref_key(item.id) == ref_key(position.requirement_id)), None)
        if requirement is None:
            raise Missing("customer-portal", f"Record requirement {position.requirement_id} of revision {revision.id}.")
        attrs = {name: getattr(requirement.attributes, name) for name in ATTRIBUTES}
        sources = {}
        for answer in self.qa:
            for amendment in answer.amendments:
                if ref_key(amendment.revision) != ref_key(revision.id) or ref_key(amendment.requirement_id) != ref_key(requirement.id):
                    continue
                for name in ATTRIBUTES:
                    value = getattr(amendment.attributes, name)
                    if value is not None:
                        attrs[name] = value
                        sources[name] = answer.number
        for name, number in sources.items():
            self.note(f"{requirement.id}: {name} amended by Q&A {number}")
        missing = [name for name in ATTRIBUTES if attrs[name] is None]
        if missing:
            raise Missing("customer-portal", f"Requirement {requirement.id}: record {', '.join(missing)}.")
        if requirement.usable_stroke:
            if not requirement.end_reserve_key:
                raise Missing("customer-portal", f"Requirement {requirement.id} is a usable stroke: record its Endreserve key.")
            found = self.latest_keyed("technical_release", "technical_releases", requirement.end_reserve_key)
            if found is None:
                raise Missing("mail", f"Find the Technikfreigabe for {requirement.end_reserve_key} and revision {revision.id}.")
            message, release = found
            if ref_key(release.revision) != ref_key(revision.id):
                raise Missing("mail", f"The latest Technikfreigabe for {requirement.end_reserve_key} names revision {release.revision}, not {revision.id}.")
            usable = D(attrs["stroke_mm"])
            attrs["stroke_mm"] = usable + 2 * D(release.reserve_mm_per_end)
            self.note(f"{requirement.id}: nominal stroke {attrs['stroke_mm']} = {usable} + 2 x {release.reserve_mm_per_end}")
        return requirement, attrs

    def quantity(self, position: Position, revision: Revision, requirement: Requirement) -> int:
        changes = [(answer, change) for answer in self.qa for change in answer.quantity_changes if change.position.strip() == position.position.strip()]
        quantity, unit, origin = position.quantity, position.unit_kind, "tender"
        if changes:
            answer, change = changes[-1]
            quantity, unit, origin = change.quantity, change.unit_kind, f"Q&A {answer.number}"
        if unit == "set":
            per_set = requirement.pieces_per_set or revision.pieces_per_set
            if not per_set:
                raise Missing("customer-portal", f"Position {position.position} is in Satz: find the pieces per set.")
            pieces = D(quantity) * per_set
            origin += f", {quantity} Satz x {per_set}"
        elif unit == "piece":
            pieces = D(quantity)
        else:
            raise Missing("tender-portal", f"Position {position.position}: the unit is neither pieces nor sets.")
        if pieces != pieces.to_integral_value() or pieces <= 0:
            raise Missing("tender-portal", f"Position {position.position}: {pieces} is not a whole number of pieces.")
        self.note(f"{position.position}: {int(pieces)} pieces ({origin})")
        return int(pieces)

    def material(self, position: str, requirement: Requirement, attrs: dict) -> Material:
        group = ref_key(requirement.catalog_group)
        candidates = [
            material for material in self.ev.materials
            if not requirement.catalog_group or not material.catalog_group or ref_key(material.catalog_group) == group
        ]
        matches, unknown = [], []
        for material in candidates:
            if material.status is None:
                unknown.append(material.number)
                continue
            if material.status != "active":
                continue
            values = {name: getattr(material.attributes, name) for name in ATTRIBUTES}
            if any(value is None for value in values.values()):
                unknown.append(material.number)
            elif all(same(name, values[name], attrs[name]) for name in ATTRIBUTES):
                matches.append(material)
        if matches:
            matches.sort(key=lambda material: material.number)
            if len(matches) > 1:
                self.warnings.append(f"{position}: several materials match, took {matches[0].number}")
            self.note(f"{position}: material {matches[0].number}")
            return matches[0]
        if unknown:
            raise Missing("erp", f"Record status and attributes of {', '.join(unknown)}.")
        searched = any(ref_key(item.catalog_group) == group and item.complete for item in self.ev.searches)
        if requirement.catalog_group and searched:
            self.note(f"{position}: no active material in {requirement.catalog_group}")
            raise NoMaterial()
        raise Missing("erp", f"Search Kataloggruppe {requirement.catalog_group or '(unknown)'} and open each listed material.")

    def base_price(self, material: Material) -> Decimal:
        valid = [
            condition for condition in material.price_conditions
            if self.day is not None and condition.valid_from <= self.day and (condition.valid_to is None or self.day <= condition.valid_to)
        ]
        if not valid:
            raise Missing("erp", f"Material {material.number}: record the price condition valid on {self.day}.")
        valid.sort(key=lambda condition: condition.valid_from)
        condition = valid[-1]
        if not condition.scales or condition.price_unit <= 0:
            raise Missing("erp", f"Material {material.number}: record the quantity scales and the price unit.")
        rate = Decimal(1)
        if condition.currency.upper() != "EUR":
            if not condition.fx_key:
                raise Missing("erp", f"Material {material.number}: the {condition.currency} condition names no FX key.")
            found = self.latest_keyed("fx_fixing", "fx_rates", condition.fx_key)
            if found is None:
                raise Missing("mail", f"Find the Devisenfixing for {condition.fx_key}.")
            message, fx = found
            if fx.currency.upper() != condition.currency.upper():
                self.warnings.append(f"{condition.fx_key}: condition is {condition.currency}, fixing is {fx.currency}")
            rate = D(fx.eur_per_unit)
            self.note(f"{material.number}: 1 {condition.currency} = {rate} EUR ({message.id})")
        self._condition = condition
        self._rate = rate
        return rate

    def scale_for(self, quantity: int) -> Decimal:
        condition = self._condition
        eligible = [scale for scale in condition.scales if scale.min_quantity <= quantity]
        if not eligible:
            raise Missing("erp", f"No price scale reaches quantity {quantity}.")
        scale = max(eligible, key=lambda item: item.min_quantity)
        per_piece = D(scale.price) / D(condition.price_unit)
        return cents(per_piece * D(self._rate))

    def mass_surcharge(self, material: Material) -> Decimal:
        if not material.surcharge_mass_kg:
            return ZERO
        if not material.metal_index:
            raise Missing("erp", f"Material {material.number} has a surcharge mass: record its metal index code.")
        messages = self.messages("metal_index")
        if not messages:
            raise Missing("mail", f"Find the Preisindizes message for {material.metal_index}.")
        message = messages[-1]
        rates = [rate for rate in message.metal_index_rates if ref_key(rate.code) == ref_key(material.metal_index)]
        if not rates:
            raise Missing("mail", f"The latest Preisindizes message does not list {material.metal_index}.")
        windowed = [rate for rate in rates if rate.valid_from or rate.valid_to]
        if windowed:
            rates = [rate for rate in windowed if (rate.valid_from or date.min) <= self.day <= (rate.valid_to or date.max)]
        if len(rates) != 1:
            raise Missing("mail", f"Record the {material.metal_index} rate whose window contains {self.day}.")
        surcharge = D(material.surcharge_mass_kg) * D(rates[0].eur_per_kg)
        self.note(f"{material.number}: surcharge {material.surcharge_mass_kg} kg x {rates[0].eur_per_kg}")
        return surcharge

    def assigned(self, position: str, name: str):
        value = None
        for answer in self.qa:
            for assignment in answer.assignments:
                if assignment.position.strip() == position.strip() and getattr(assignment, name) is not None:
                    value = getattr(assignment, name)
                    self.note(f"{position}: {name} {value} from Q&A {answer.number}")
        return value

    def line(self, revision: Revision, position: Position) -> tuple[Priced, str | None, str | None]:
        requirement, attrs = self.requirement(revision, position)
        quantity = self.quantity(position, revision, requirement)
        material = self.material(position.position, requirement, attrs)
        self.base_price(material)
        base = self.scale_for(quantity)
        mass = self.mass_surcharge(material)
        for name in ("stock_pieces", "stock_lead_time_weeks", "production_lead_time_weeks"):
            if getattr(material, name) is None:
                raise Missing("erp", f"Material {material.number}: record {name.replace('_', ' ')}.")
        scope = SCOPE_BASE
        if group := self.assigned(position.position, "condition_group"):
            found = self.latest_keyed("condition_rules", "condition_rules", group)
            if found is None:
                raise Missing("mail", f"Find the Konditionsregeln for {group}.")
            scope = found[1].scope
        carton_count_price = ZERO
        carton_size = None
        if tariff := self.assigned(position.position, "packaging_tariff"):
            found = self.latest_keyed("packaging_tariffs", "packaging_tariffs", tariff)
            if found is None:
                raise Missing("mail", f"Find the Verpackungstarife for {tariff}.")
            if not material.pieces_per_carton:
                raise Missing("erp", f"Material {material.number}: record the pieces per carton.")
            carton_size = material.pieces_per_carton
            carton_count_price = D(found[1].eur_per_carton)
        commitment = None
        if key := self.assigned(position.position, "delivery_commitment"):
            found = self.latest_keyed("delivery_commitments", "delivery_commitments", key)
            if found is None:
                raise Missing("mail", f"Find the Terminfreigabe for {key}.")
            entry = found[1]
            commitment = (entry.material, entry.max_pieces, entry.lead_time_weeks)
        pool = self.assigned(position.position, "setup_pool")
        if pool and material.allocation_mass_g is None:
            raise Missing("erp", f"Material {material.number}: record the allocation mass.")
        bonus = self.assigned(position.position, "bonus_plan")
        setup = self.assigned(position.position, "setup_fee_eur") or ZERO
        packaging = ZERO if not carton_size else Decimal(math.ceil(quantity / carton_size)) * carton_count_price
        return Priced(
            position=position.position,
            quantity=quantity,
            material=material.number,
            base=base,
            mass=mass,
            scope=scope,
            setup=D(setup),
            packaging=packaging,
            stock=material.stock_pieces,
            stock_weeks=material.stock_lead_time_weeks,
            production_weeks=material.production_lead_time_weeks,
            commitment=commitment,
            pool=bool(pool),
            allocation_mass=D(material.allocation_mass_g or 0),
            bonus=bool(bonus),
        ), (pool or None), (bonus or None)

    def approval(self) -> tuple[Decimal, Decimal | None]:
        if self.strict and not self.ev.mail_complete:
            raise Missing("mail", "Search the inbox and the archive for this tender reference, including look-alike and older approvals.")
        found = self.messages("discount_approval")
        if not found:
            self.note("discount: no director approval, 0%")
            if not self.ev.mail_complete:
                self.warnings.append("mailbox search was not marked finished; discount assumed 0%")
            return ZERO, None
        message = found[-1]
        percent = D(message.discount_percent or 0)
        self.note(f"discount: {percent}% from {message.id}")
        return percent, message.min_order_value_eur

    def pool_fee(self, keys: set[str]) -> Decimal:
        if not keys:
            return ZERO
        if len(keys) > 1:
            raise Missing("tender-portal", f"Several setup pools {sorted(keys)}.")
        key = next(iter(keys))
        found = self.latest_keyed("setup_pools", "setup_pools", key)
        if found is None:
            raise Missing("mail", f"Find the Rüstpools mail for {key}.")
        if found[1].allocation != LARGEST_REMAINDER:
            raise Missing("mail", f"The cent-allocation rule for {key} is not the largest-remainder rule.")
        self.note(f"setup pool {key}: {found[1].fee_eur} EUR")
        return D(found[1].fee_eur)

    def bonus_bands(self, keys: set[str]) -> list[tuple[Decimal, Decimal | None, Decimal]]:
        if not keys:
            return []
        if len(keys) > 1:
            raise Missing("tender-portal", f"Several bonus plans {sorted(keys)}.")
        key = next(iter(keys))
        found = self.latest_keyed("project_bonus", "bonus_plans", key)
        if found is None:
            raise Missing("mail", f"Find the Projektbonus mail for {key}.")
        if found[1].allocation != LARGEST_REMAINDER:
            raise Missing("mail", f"The allocation rule for {key} is not the largest-remainder rule.")
        return [(band.lower_eur, band.upper_eur, band.percent) for band in found[1].bands]


def lead_time(line: Priced, demand: dict[str, int]) -> int:
    pooled = demand[line.material]
    if line.commitment is not None:
        material, ceiling, weeks = line.commitment
        if ref_key(material) == ref_key(line.material) and pooled <= ceiling:
            return weeks
    return line.stock_weeks if line.stock >= pooled else line.production_weeks


def price_lines(reference: str, lines: list[Priced], percent: Decimal, threshold: Decimal | None,
                pool_fee: Decimal, bands: list[tuple[Decimal, Decimal | None, Decimal]]) -> dict:
    if any(line.scope not in {SCOPE_BASE, SCOPE_BOTH, SCOPE_NONE} for line in lines):
        raise ValueError("unknown discount scope")
    goods = cents(sum((line.quantity * line.base for line in lines), ZERO))
    applied = percent
    if threshold is not None and goods < D(threshold):
        applied = ZERO
    factor = 1 - applied / 100
    demand: dict[str, int] = {}
    for line in lines:
        demand[line.material] = demand.get(line.material, 0) + line.quantity
    weights = {line.position: line.quantity * line.allocation_mass for line in lines if line.pool}
    if pool_fee > 0 and sum(weights.values(), ZERO) == 0:
        raise ValueError("setup pool has no allocation weight")
    pool = allocate_cents(pool_fee, weights)
    eligible = {line.position: line.quantity * line.base for line in lines if line.bonus}
    bonus = allocate_cents(project_bonus(bands, sum(eligible.values(), ZERO)), eligible) if eligible else {}
    priced = []
    units: list[Decimal] = []
    for line in lines:
        quantity = Decimal(line.quantity)
        setup = line.setup + pool.get(line.position, ZERO)
        rebate = bonus.get(line.position, ZERO)
        discounted_base = line.base * (factor if line.scope != SCOPE_NONE else 1)
        discounted_mass = line.mass * (factor if line.scope == SCOPE_BOTH else 1)
        unit = cents(discounted_base + discounted_mass + (setup + line.packaging - rebate) / quantity)
        units.append(unit)
        priced.append({
            "position": line.position,
            "material_number": line.material,
            "quantity": line.quantity,
            "unit_price": money(unit),
            "lead_time_weeks": lead_time(line, demand),
            "base_unit_price": money(line.base),
            "surcharge_per_unit": money(cents(line.mass + setup / quantity)),
            "packaging_per_unit": money(cents(line.packaging / quantity)),
            "rebate_total": money(rebate),
        })
    priced.sort(key=lambda row: position_key(row["position"]))
    units_by_position = {line.position: unit for line, unit in zip(lines, units)}
    total = cents(sum((Decimal(row["quantity"]) * units_by_position[row["position"]] for row in priced), ZERO))
    return {
        "tender_reference": reference,
        "decision": "bid",
        "currency": "EUR",
        "discount_percent": float(applied),
        "positions": priced,
        "total_net": money(total),
    }


def resolve(evidence: Evidence, strict: bool = True) -> Resolution:
    kernel = Kernel(evidence, strict=strict)
    reference = evidence.case.tender_reference
    if not reference or evidence.case.scenario_date is None:
        return Resolution(gaps=[Gap("tender-portal", "Record the tender reference and the scenario date.")], trace=kernel.trace)
    if not evidence.case.bidder_mail_domain:
        return Resolution(gaps=[Gap("mail", "Record the bidder mailbox domain.")], trace=kernel.trace, warnings=kernel.warnings)
    positions = sorted(evidence.positions, key=lambda position: position_key(position.position))
    gaps = kernel._undated_effects()
    if not positions:
        gaps.append(Gap("tender-portal", "Record every bill-of-quantities position."))
        return Resolution(gaps=gaps, trace=kernel.trace, warnings=kernel.warnings)
    blocked = {gap.position for gap in gaps if gap.position}
    if any(gap.position is None and "withdraws" in gap.question for gap in gaps):
        return Resolution(gaps=gaps, trace=kernel.trace, warnings=kernel.warnings)
    try:
        revision = kernel.governing_revision()
    except Missing as missing:
        return Resolution(omitted=[position.position for position in positions], gaps=gaps + [missing.gap], trace=kernel.trace, warnings=kernel.warnings)

    lines: list[Priced] = []
    pools: set[str] = set()
    bonuses: set[str] = set()
    missing_material: list[str] = []
    for position in positions:
        if position.position in blocked:
            continue
        try:
            line, pool, bonus = kernel.line(revision, position)
        except NoMaterial:
            missing_material.append(position.position)
            continue
        except Missing as missing:
            missing.gap.position = missing.gap.position or position.position
            gaps.append(missing.gap)
            continue
        lines.append(line)
        if pool:
            pools.add(pool)
        if bonus:
            bonuses.add(bonus)
    if missing_material:
        kernel.note(f"decision: no_bid, no material for {', '.join(missing_material)}")
        answer = no_bid(reference)
        answer["notes"] = _notes(kernel)
        return Resolution(answer, [], gaps, kernel.trace, kernel.warnings)
    if not lines:
        return Resolution(omitted=[position.position for position in positions], gaps=gaps, trace=kernel.trace, warnings=kernel.warnings)
    try:
        percent, threshold = kernel.approval()
        fee = kernel.pool_fee(pools)
        bands = kernel.bonus_bands(bonuses)
        answer = price_lines(reference, lines, percent, threshold, fee, bands)
    except Missing as missing:
        return Resolution(omitted=[position.position for position in positions], gaps=gaps + [missing.gap], trace=kernel.trace, warnings=kernel.warnings)
    except ValueError as error:
        gaps.append(Gap("mail", str(error)))
        return Resolution(omitted=[position.position for position in positions], gaps=gaps, trace=kernel.trace, warnings=kernel.warnings)
    omitted = []
    for gap in gaps:
        if gap.position and gap.position not in omitted and all(line.position != gap.position for line in lines):
            omitted.append(gap.position)
    answer["notes"] = _notes(kernel)
    kernel.note(f"decision: bid, {len(lines)} of {len(positions)} positions")
    return Resolution(answer, omitted, gaps, kernel.trace, kernel.warnings)


def _notes(kernel: Kernel) -> str:
    text = " | ".join(kernel.trace[-12:])
    return text[:4000]


def validate_answer(answer: dict) -> list[str]:
    problems = []
    for key in ("tender_reference", "decision", "currency", "discount_percent", "positions", "total_net"):
        if key not in answer:
            problems.append(f"missing {key}")
    if answer.get("decision") not in {"bid", "no_bid"}:
        problems.append("decision")
    if answer.get("currency") != "EUR":
        problems.append("currency")
    total = ZERO
    for position in answer.get("positions") or []:
        for key in ("position", "material_number", "quantity", "unit_price", "lead_time_weeks"):
            if key not in position:
                problems.append(f"{position.get('position')}: missing {key}")
        if "unit_price" in position and "quantity" in position:
            total += D(position["quantity"]) * D(str(position["unit_price"]))
    if answer.get("decision") == "bid" and "total_net" in answer:
        if cents(total) != cents(D(str(answer["total_net"]))):
            problems.append("total does not match the positions")
    return problems
