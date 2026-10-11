"""The brief's rules over the evidence: which records govern, then pricing.py for the numbers. Pure code, milliseconds.

`resolve(evidence)` returns the answer for the tender portal, a trace of which record decided what, the agents' picks
it disagrees with, and the gaps: evidence that is missing, each phrased as a follow-up question for a research agent.
A position with a gap is left out of the answer (the bid form allows omitting positions); no_bid is only decided
when a Kataloggruppe was searched completely and no active material matches.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pricing
from evidence import Evidence, Material, Message, QAAnswer, Requirement, SpecRevision, TenderPosition

# Brief, "Authority and time" and rules 5 and 12: who may issue which record (local part + bidder mailbox domain).
DEFAULT_SENDERS = {
    "discount_approval": "leitung", "condition_rules": "kalkulation", "metal_index": "kalkulation",
    "setup_pools": "kalkulation", "technical_release": "technik", "packaging_tariffs": "logistik",
    "delivery_commitments": "logistik", "fx_fixing": "finanzen", "project_bonus": "finanzen",
}


def senders_from_brief(brief: str) -> dict[str, str] | None:
    """Local parts the brief names next to a kind hint. None when it names none."""
    text = brief.lower()
    hints = {
        "discount_approval": ("leitung", "discount", "rabatt", "sales director"),
        "condition_rules": ("kalkulation", "konditionsregeln"),
        "metal_index": ("preisindizes", "metal index"),
        "setup_pools": ("rüstpool", "ruestpool"),
        "technical_release": ("technik", "technikfreigabe"),
        "packaging_tariffs": ("verpackungstarife", "packaging"),
        "delivery_commitments": ("terminfreigabe",),
        "fx_fixing": ("devisenfixing", "fx"),
        "project_bonus": ("projektbonus",),
    }
    found: dict[str, str] = {}
    for match in re.finditer(r"\b([a-z][\w.+-]*)@", text):
        local = match.group(1)
        window = text[max(0, match.start() - 48): match.end() + 48]
        for kind, default in DEFAULT_SENDERS.items():
            if local == default:
                found[kind] = local
        for kind, words in hints.items():
            if kind not in found and any(w in window for w in words):
                found[kind] = local
    return found or None
ATTRIBUTES = ("bore_mm", "stroke_mm", "pressure_bar", "mounting", "certification")
NO_CERTIFICATION = {"", "none", "keine", "keine gefordert", "nicht gefordert", "ohne", "-", "–", "—", "n/a"}
LARGEST_REMAINDER = "floor_largest_remainder_ascending_position"
INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))


@dataclass
class Gap:
    site: str          # where the missing evidence most likely is
    question: str      # a follow-up task for a research agent
    position: str | None = None


@dataclass
class Resolution:
    answer: dict | None                     # the tender portal answer, None when no position could be priced
    omitted: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.gaps


class Missing(Exception):
    def __init__(self, site: str, question: str):
        super().__init__(question)
        self.gap = Gap(site, question)


class NoMaterial(Exception):
    """A completely searched Kataloggruppe without an active exactly matching material: the bid is no_bid."""


# --- normalization -----------------------------------------------------------------------------------------------

def ref(value: str | None) -> str:
    """References compare exactly, apart from invisible characters and whitespace."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "").translate(INVISIBLE)).upper()


def text(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "").translate(INVISIBLE)).strip().casefold()


def certification(value: str | None) -> str:
    t = text(value)
    return "none" if t in NO_CERTIFICATION else t.replace(" ", "")


def same(attribute: str, a, b) -> bool:
    if attribute in ("mounting",):
        return text(a) == text(b)
    if attribute == "certification":
        return certification(a) == certification(b)
    return Decimal(a) == Decimal(b)


def address(sender: str) -> str:
    found = re.search(r"[\w.+-]+@[\w.-]+", sender or "")
    return found.group(0).lower() if found else text(sender)


def position_key(position: str):
    return pricing._position_key(position.strip())


# --- the rules ---------------------------------------------------------------------------------------------------

class Resolver:
    def __init__(self, evidence: Evidence):
        self.ev = evidence
        self.case = evidence.case
        self.reference = ref(self.case.tender_reference)
        self.when = self.case.scenario_date
        self.day = self.when.date()
        self.trace: list[str] = []
        self.warnings: list[str] = []
        self.qa = self._valid_qa()

    def note(self, line: str):
        self.trace.append(line)

    # records that apply at all: exact reference, no later than the scenario date, authorized sender

    def _valid_qa(self) -> list[QAAnswer]:
        dated = [a for a in self.ev.qa if a.date <= self.when and (a.reference is None or ref(a.reference) ==
                                                                    self.reference)]
        withdrawn = {n.strip() for a in dated for n in a.withdraws}
        for number in sorted(withdrawn):
            self.note(f"Q&A {number} withdrawn")
        return sorted((a for a in dated if a.number.strip() not in withdrawn), key=lambda a: a.date)

    def messages(self, kind: str) -> list[Message]:
        local = (self.case.authorized_senders or {}).get(kind) or DEFAULT_SENDERS[kind]
        sender = f"{local}@{self.case.bidder_mail_domain}".lower()
        return sorted((m for m in self.ev.messages if m.kind == kind and address(m.sender) == sender
                       and ref(m.reference) == self.reference and m.date <= self.when), key=lambda m: m.date)

    def latest_entry(self, kind: str, entries: str, key: str, accept=lambda entry: True):
        """The newest applicable record for a key: later messages replace earlier ones, also when a later one
        fails a condition (nothing older is revived)."""
        found = None
        for message in self.messages(kind):
            for entry in getattr(message, entries):
                if ref(entry.key) == ref(key) and accept(entry):
                    found = (message, entry)
        return found

    # specification

    def governing_revision(self) -> SpecRevision:
        current = {r.id: r for r in self.ev.revisions if r.marking == "current"}
        if len(current) != 1:
            found = ", ".join(current) or "none"
            raise Missing("customer-portal", f"Exactly one specification revision must be marked current (found: "
                                             f"{found}). Open each revision and record its marking.")
        revision = next(iter(current.values()))
        if self.ev.picks.governing_revision and self.ev.picks.governing_revision != revision.id:
            self.warnings.append(f"agents picked revision {self.ev.picks.governing_revision}, the marking says "
                                 f"{revision.id}")
        self.note(f"specification: revision {revision.id} is marked current")
        return revision

    def requirement(self, revision: SpecRevision, position: TenderPosition) -> tuple[Requirement, dict]:
        if not position.requirement_id:
            raise Missing("tender-portal", f"Which specification requirement does position {position.position} "
                                           "refer to?")
        req = next((r for r in revision.requirements if ref(r.id) == ref(position.requirement_id)), None)
        if req is None:
            raise Missing("customer-portal", f"Record requirement {position.requirement_id} of revision "
                                             f"{revision.id} with all attributes and its Kataloggruppe.")
        attrs = {a: getattr(req, a) for a in ATTRIBUTES}
        sources = {}
        for answer in self.qa:  # oldest first: the latest dated amendment wins per attribute
            for amendment in answer.amendments:
                if ref(amendment.revision) == ref(revision.id) and ref(amendment.requirement_id) == ref(req.id):
                    for a in ATTRIBUTES:
                        value = getattr(amendment.attributes, a)
                        if value is not None:
                            attrs[a], sources[a] = value, answer.number
        for a, number in sources.items():
            self.note(f"{req.id}: {a} = {attrs[a]} from amendment Q&A {number}")
        missing = [a for a in ATTRIBUTES if attrs[a] is None]
        if missing:
            raise Missing("customer-portal", f"Requirement {req.id} of revision {revision.id}: record "
                                             f"{', '.join(missing)}.")
        if req.usable_stroke:
            if not req.end_reserve_key:
                raise Missing("customer-portal", f"Requirement {req.id} is a usable stroke: record its Endreserve key.")
            found = self.latest_entry("technical_release", "technical_releases", req.end_reserve_key,
                                      lambda e: ref(e.revision) == ref(revision.id))
            if found is None:
                raise Missing("mail", f"Find the Technikfreigabe for key {req.end_reserve_key} and revision "
                                      f"{revision.id} (technik@{self.case.bidder_mail_domain}).")
            message, release = found
            usable = Decimal(attrs["stroke_mm"])
            attrs["stroke_mm"] = usable + 2 * release.reserve_mm_per_end
            self.note(f"{req.id}: nominal stroke {attrs['stroke_mm']} = usable {usable} + 2 x "
                      f"{release.reserve_mm_per_end} ({message.id})")
        return req, attrs

    # tender

    def quantity(self, position: TenderPosition, revision: SpecRevision, req: Requirement) -> int:
        changes = [(a, c) for a in self.qa for c in a.quantity_changes
                   if c.position.strip() == position.position.strip()]
        quantity, unit, origin = position.quantity, position.unit_kind, "tender"
        if changes:
            answer, change = changes[-1]
            ties = {c.quantity for a, c in changes if a.date == answer.date}
            if len(ties) > 1:
                self.warnings.append(f"{position.position}: same-dated quantity changes disagree: {sorted(ties)}")
            quantity, unit, origin = change.quantity, change.unit_kind, f"Q&A {answer.number}"
        if unit == "set":
            per_set = req.pieces_per_set or revision.pieces_per_set
            if not per_set:
                raise Missing("customer-portal", f"Position {position.position} is in Satz: find the pieces per set "
                                                 f"in revision {revision.id} (requirement {req.id}).")
            pieces = quantity * per_set
            origin += f", {quantity} Satz x {per_set}"
        elif unit == "piece":
            pieces = quantity
        else:
            raise Missing("tender-portal", f"Position {position.position}: the unit is neither pieces nor sets.")
        if pieces != int(pieces) or pieces <= 0:
            raise Missing("tender-portal", f"Position {position.position}: {pieces} is not a whole number of pieces.")
        self.note(f"{position.position}: {int(pieces)} pieces ({origin})")
        return int(pieces)

    # ERP

    def material(self, position: str, req: Requirement, attrs: dict) -> Material:
        group = ref(req.catalog_group)
        candidates = [m for m in self.ev.materials if not req.catalog_group or not m.catalog_group
                      or ref(m.catalog_group) == group]
        matches, unknown = [], []
        for m in candidates:
            if m.status != "active":
                continue
            values = {a: getattr(m.attributes, a) for a in ATTRIBUTES}
            if any(v is None for v in values.values()):
                unknown.append(m.number)
            elif all(same(a, values[a], attrs[a]) for a in ATTRIBUTES):
                matches.append(m)
        if matches:
            matches.sort(key=lambda m: m.number)
            if len(matches) > 1:
                self.warnings.append(f"{position}: several active materials match ({[m.number for m in matches]}), "
                                     f"took {matches[0].number}")
            self.note(f"{position}: material {matches[0].number} matches {req.id} exactly")
            return matches[0]
        if unknown:
            raise Missing("erp", f"Record bore, stroke, pressure, mounting and certification of {', '.join(unknown)}.")
        searched = any(ref(s.catalog_group) == group and s.complete for s in self.ev.erp_searches)
        if req.catalog_group and searched:
            self.note(f"{position}: no active material in {req.catalog_group} matches {req.id} "
                      f"({', '.join(f'{a}={attrs[a]}' for a in ATTRIBUTES)})")
            raise NoMaterial(position)
        raise Missing("erp", f"Search the ERP for Kataloggruppe {req.catalog_group or '(unknown)'}, open every "
                             f"listed material and record it; the requirement is "
                             f"{', '.join(f'{a}={attrs[a]}' for a in ATTRIBUTES)}.")

    def condition(self, material: Material) -> pricing.PriceCondition:
        valid = [c for c in material.price_conditions
                 if c.valid_from <= self.day and (c.valid_to is None or self.day <= c.valid_to)]
        if not valid:
            raise Missing("erp", f"Material {material.number}: record the price condition valid on {self.day} "
                                 "with its scales, price unit and currency.")
        valid.sort(key=lambda c: c.valid_from)
        if len(valid) > 1:
            self.warnings.append(f"{material.number}: several conditions valid on {self.day}, took the newest")
        c = valid[-1]
        rate = Decimal(1)
        if c.currency.upper() != "EUR":
            if not c.fx_key:
                raise Missing("erp", f"Material {material.number}: the {c.currency} condition names no FX key.")
            found = self.latest_entry("fx_fixing", "fx_rates", c.fx_key)
            if found is None:
                raise Missing("mail", f"Find the Devisenfixing for {c.fx_key} (finanzen@{self.case.bidder_mail_domain}).")
            message, fx = found
            if fx.currency.upper() != c.currency.upper():
                self.warnings.append(f"{c.fx_key}: condition in {c.currency}, fixing for {fx.currency}")
            rate = fx.eur_per_unit
            self.note(f"{material.number}: 1 {c.currency} = {rate} EUR ({message.id})")
        self.note(f"{material.number}: condition valid {c.valid_from}..{c.valid_to or 'open'}")
        return pricing.PriceCondition(scales=[pricing.PriceScale(s.min_quantity, s.price) for s in c.scales],
                                      currency=c.currency, price_unit=c.price_unit, eur_per_unit=rate)

    def mass_surcharge(self, material: Material) -> Decimal:
        if not material.surcharge_mass_kg:
            return pricing.ZERO
        if not material.metal_index:
            raise Missing("erp", f"Material {material.number} has a surcharge mass: record its metal index code.")
        latest = None
        for message in self.messages("metal_index"):  # the latest Preisindizes message listing the code
            rates = [r for r in message.metal_index_rates if ref(r.code) == ref(material.metal_index)]
            if rates:
                latest = (message, rates)
        if latest is None:
            raise Missing("mail", f"Find the Preisindizes for {material.metal_index} "
                                  f"(kalkulation@{self.case.bidder_mail_domain}).")
        message, rates = latest
        windowed = [r for r in rates if r.valid_from or r.valid_to]
        if windowed:
            rates = [r for r in windowed if (r.valid_from or date.min) <= self.day <= (r.valid_to or date.max)]
        if len(rates) != 1:
            raise Missing("mail", f"{message.id}: record the {material.metal_index} rate whose window contains "
                                  f"{self.day}.")
        surcharge = material.surcharge_mass_kg * rates[0].eur_per_kg
        self.note(f"{material.number}: surcharge {material.surcharge_mass_kg} kg x {rates[0].eur_per_kg} EUR/kg "
                  f"({message.id})")
        return surcharge

    # Q&A keys

    def assigned(self, position: str, name: str):
        value, origin = None, None
        for answer in self.qa:
            for a in answer.assignments:
                if a.position.strip() == position.strip() and getattr(a, name) is not None:
                    value, origin = getattr(a, name), answer.number
        if value is not None:
            self.note(f"{position}: {name} {value} (Q&A {origin})")
        return value

    def line(self, revision: SpecRevision, position: TenderPosition) -> tuple[pricing.Line, str | None, str | None]:
        p = position.position
        req, attrs = self.requirement(revision, position)
        quantity = self.quantity(position, revision, req)
        material = self.material(p, req, attrs)
        condition = self.condition(material)
        surcharge = self.mass_surcharge(material)
        for name in ("stock_pieces", "stock_lead_time_weeks", "production_lead_time_weeks"):
            if getattr(material, name) is None:
                raise Missing("erp", f"Material {material.number}: record {name.replace('_', ' ')}.")

        scope = pricing.SCOPE_BASE
        if group := self.assigned(p, "condition_group"):
            found = self.latest_entry("condition_rules", "condition_rules", group)
            if found is None:
                raise Missing("mail", f"Find the Konditionsregeln for {group} (kalkulation@"
                                      f"{self.case.bidder_mail_domain}).")
            scope = found[1].scope
            self.note(f"{p}: {group} -> discount on {scope} ({found[0].id})")

        carton_size, carton_price = None, pricing.ZERO
        if tariff := self.assigned(p, "packaging_tariff"):
            found = self.latest_entry("packaging_tariffs", "packaging_tariffs", tariff)
            if found is None:
                raise Missing("mail", f"Find the Verpackungstarife for {tariff} (logistik@"
                                      f"{self.case.bidder_mail_domain}).")
            if not material.pieces_per_carton:
                raise Missing("erp", f"Material {material.number}: record the pieces per carton (Packinhalt).")
            carton_size, carton_price = material.pieces_per_carton, found[1].eur_per_carton

        commitment = None
        if key := self.assigned(p, "delivery_commitment"):
            found = self.latest_entry("delivery_commitments", "delivery_commitments", key)
            if found is None:
                raise Missing("mail", f"Find the Terminfreigabe for {key} (logistik@{self.case.bidder_mail_domain}).")
            c = found[1]
            commitment = pricing.Commitment(c.material, c.max_pieces, c.lead_time_weeks)

        pool = self.assigned(p, "setup_pool")
        if pool and material.allocation_mass_g is None:
            raise Missing("erp", f"Material {material.number}: record the allocation mass (Umlagegewicht).")
        bonus = self.assigned(p, "bonus_plan")

        return pricing.Line(
            position=p, quantity=quantity, material=material.number, condition=condition,
            stock=material.stock_pieces, stock_lead_time_weeks=material.stock_lead_time_weeks,
            production_lead_time_weeks=material.production_lead_time_weeks, commitment=commitment,
            mass_surcharge_per_unit=surcharge, setup_fee=self.assigned(p, "setup_fee_eur") or pricing.ZERO,
            discount_scope=scope, carton_size=carton_size, carton_price=carton_price,
            setup_pool=bool(pool), allocation_mass=material.allocation_mass_g or pricing.ZERO,
            bonus_eligible=bool(bonus)), pool, bonus

    # order level

    def approval(self) -> pricing.Approval | None:
        approvals = [m for m in self.messages("discount_approval") if m.discount_percent is not None]
        if not approvals:
            self.note("discount: no approval from the director for this reference, 0 %")
            return None
        m = approvals[-1]
        if self.ev.picks.discount_message_id and self.ev.picks.discount_message_id != m.id:
            self.warnings.append(f"agents picked approval {self.ev.picks.discount_message_id}, the rules give {m.id}")
        condition = f", if the order value reaches {m.min_order_value_eur} EUR" if m.min_order_value_eur else ""
        self.note(f"discount: {m.discount_percent} % from {m.id} ({m.date:%Y-%m-%d %H:%M}){condition}")
        return pricing.Approval(m.discount_percent, m.min_order_value_eur)

    def pool_fee(self, keys: set[str]) -> Decimal:
        if not keys:
            return pricing.ZERO
        if len(keys) > 1:
            raise Missing("tender-portal", f"Several setup pools {sorted(keys)}: only one pool per order is handled.")
        key = keys.pop()
        found = self.latest_entry("setup_pools", "setup_pools", key)
        if found is None:
            raise Missing("mail", f"Find the Rüstpools mail for {key} (kalkulation@{self.case.bidder_mail_domain}).")
        if found[1].allocation != LARGEST_REMAINDER:
            raise Missing("mail", f"{found[0].id}: the {key} cent-allocation rule is not the largest-remainder rule.")
        self.note(f"setup pool {key}: {found[1].fee_eur} EUR ({found[0].id})")
        return found[1].fee_eur

    def bonus_bands(self, keys: set[str]) -> list[pricing.BonusBand]:
        if not keys:
            return []
        if len(keys) > 1:
            raise Missing("tender-portal", f"Several bonus plans {sorted(keys)}: only one per order is handled.")
        key = keys.pop()
        found = self.latest_entry("project_bonus", "bonus_plans", key)
        if found is None:
            raise Missing("mail", f"Find the Projektbonus mail for {key} (finanzen@{self.case.bidder_mail_domain}).")
        if found[1].allocation != LARGEST_REMAINDER:
            raise Missing("mail", f"{found[0].id}: the {key} allocation rule is not the largest-remainder rule.")
        self.note(f"project bonus {key}: {len(found[1].bands)} bands ({found[0].id})")
        return [pricing.BonusBand(b.lower_eur, b.upper_eur, b.percent) for b in found[1].bands]


def resolve(evidence: Evidence) -> Resolution:
    r = Resolver(evidence)
    reference = evidence.case.tender_reference
    positions = sorted(evidence.positions, key=lambda p: position_key(p.position))
    if not positions:
        gap = Gap("tender-portal", "Record every position of the bill of quantities.")
        return Resolution(None, gaps=[gap], trace=r.trace, warnings=r.warnings)
    try:
        revision = r.governing_revision()
    except Missing as missing:
        return Resolution(None, [p.position for p in positions], [missing.gap], r.trace, r.warnings)

    lines, pools, bonuses, gaps, no_material = [], set(), set(), [], []
    for position in positions:
        try:
            line, pool, bonus = r.line(revision, position)
        except NoMaterial:
            no_material.append(position.position)
            continue
        except Missing as missing:
            missing.gap.position = position.position
            gaps.append(missing.gap)
            continue
        lines.append(line)
        pools |= {pool} if pool else set()
        bonuses |= {bonus} if bonus else set()

    for p, picked in evidence.picks.materials.items():
        ours = next((l.material for l in lines if l.position == p), None if p in no_material else "?")
        if ours != "?" and ours != picked:
            r.warnings.append(f"{p}: agents picked material {picked}, the rules give {ours}")
    for p, picked in evidence.picks.final_quantities.items():
        ours = next((l.quantity for l in lines if l.position == p), None)
        if ours is not None and ours != picked:
            r.warnings.append(f"{p}: agents computed {picked} pieces, the rules give {ours}")

    if no_material:
        r.note(f"decision: no_bid, no active matching material for {', '.join(no_material)}")
        return Resolution(pricing.no_bid(reference), [], gaps, r.trace, r.warnings)

    try:
        order = pricing.Order(reference, lines, approval=r.approval(), setup_pool_fee=r.pool_fee(pools),
                              bonus_bands=r.bonus_bands(bonuses))
    except Missing as missing:
        return Resolution(None, [p.position for p in positions], gaps + [missing.gap], r.trace, r.warnings)
    omitted = [g.position for g in gaps if g.position]
    if omitted and order.approval and order.approval.min_order_value is not None:
        r.warnings.append("the discount threshold was checked without the omitted positions")
    answer = pricing.price_order(order) if lines else None
    r.note(f"decision: bid with {len(lines)} of {len(positions)} positions" if lines else "nothing to price")
    return Resolution(answer, omitted, gaps, r.trace, r.warnings)
