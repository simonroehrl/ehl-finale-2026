"""Typed evidence. Agents store what a page said; the kernel decides what governs.

Records are grouped by what they are, not by which website produced them. The private dataset can
split the same facts across different sites.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
KINDS = {
    "discount_approval", "condition_rules", "metal_index", "technical_release", "packaging_tariffs",
    "delivery_commitments", "fx_fixing", "setup_pools", "project_bonus", "signing_tan", "other",
}
SCOPES = {"base", "base_and_surcharge", "none"}
ALLOCATIONS = {"floor_largest_remainder_ascending_position", "other"}


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "").translate(INVISIBLE)).strip()


def ref_key(value: str | None) -> str:
    return re.sub(r"\s+", "", clean(value)).upper()


def dec(value) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip().replace(" ", "").replace("'", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def as_int(value) -> int | None:
    number = dec(value)
    if number is None:
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def as_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def as_datetime(value) -> tuple[datetime | None, bool]:
    """Return the timestamp and whether the source stated a date without a time."""
    if value is None or value == "":
        return None, False
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)), False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc), True
    text = str(value).strip().replace("Z", "+00:00")
    date_only = "T" not in text and " " not in text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        day = as_date(text)
        if day is None:
            return None, False
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc), True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed, date_only


def _flag(value) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "ja", "wahr"}
    return bool(value)


def _enum(value: str | None, allowed: set[str], default: str) -> str:
    text = clean(value).casefold().replace(" ", "_").replace("-", "_")
    return text if text in allowed else default


def marking_of(value: str | None) -> str:
    text = clean(value).casefold()
    if any(word in text for word in ("superseded", "veraltet", "ersetzt", "obsolete", "ungültig", "ungueltig", "nicht gültig")):
        return "superseded"
    if text in {"current", "gültig", "gueltig", "valid", "aktuell"} or text.startswith("gültig"):
        return "current"
    return "unknown"


def status_of(value: str | None) -> str | None:
    text = clean(value).casefold()
    if text in {"active", "aktiv", "frei", "freigegeben", "released"}:
        return "active"
    if text in {"blocked", "gesperrt", "inaktiv", "inactive", "obsolete", "gesperrt."}:
        return "blocked"
    if not text:
        return None
    return "other"


def unit_of(raw: str | None, explicit: str | None = None) -> str:
    if explicit in {"piece", "set", "other"}:
        return explicit
    text = clean(raw).casefold()
    if any(word in text for word in ("satz", "sätze", "saetze", "set")):
        return "set"
    if any(word in text for word in ("stück", "stueck", "stk", "piece", "pcs")) or text in {"st", "st."}:
        return "piece"
    return "other"


@dataclass
class Attributes:
    bore_mm: Decimal | None = None
    stroke_mm: Decimal | None = None
    pressure_bar: Decimal | None = None
    mounting: str | None = None
    certification: str | None = None


@dataclass
class Position:
    position: str
    quantity: Decimal
    unit_raw: str = ""
    unit_kind: str = "other"
    requirement_id: str | None = None
    locator: str | None = None


@dataclass
class QuantityChange:
    position: str
    quantity: Decimal
    unit_kind: str = "piece"


@dataclass
class Amendment:
    revision: str
    requirement_id: str
    attributes: Attributes = field(default_factory=Attributes)


@dataclass
class Assignment:
    position: str
    condition_group: str | None = None
    packaging_tariff: str | None = None
    delivery_commitment: str | None = None
    setup_pool: str | None = None
    bonus_plan: str | None = None
    setup_fee_eur: Decimal | None = None


@dataclass
class QA:
    number: str
    date: datetime | None
    reference: str | None = None
    text: str = ""
    withdraws: list[str] = field(default_factory=list)
    quantity_changes: list[QuantityChange] = field(default_factory=list)
    amendments: list[Amendment] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    locator: str | None = None


@dataclass
class Requirement:
    id: str
    catalog_group: str | None = None
    usable_stroke: bool = False
    end_reserve_key: str | None = None
    pieces_per_set: int | None = None
    attributes: Attributes = field(default_factory=Attributes)


@dataclass
class Revision:
    id: str
    marking: str = "unknown"
    pieces_per_set: int | None = None
    requirements: list[Requirement] = field(default_factory=list)
    locator: str | None = None


@dataclass
class Scale:
    min_quantity: int
    price: Decimal


@dataclass
class Condition:
    valid_from: date
    valid_to: date | None = None
    currency: str = "EUR"
    fx_key: str | None = None
    price_unit: int = 1
    scales: list[Scale] = field(default_factory=list)


@dataclass
class Material:
    number: str
    status: str | None = None
    catalog_group: str | None = None
    attributes: Attributes = field(default_factory=Attributes)
    stock_pieces: int | None = None
    stock_lead_time_weeks: int | None = None
    production_lead_time_weeks: int | None = None
    pieces_per_carton: int | None = None
    allocation_mass_g: Decimal | None = None
    surcharge_mass_kg: Decimal | None = None
    metal_index: str | None = None
    price_conditions: list[Condition] = field(default_factory=list)
    locator: str | None = None


@dataclass
class Search:
    catalog_group: str
    material_numbers: list[str] = field(default_factory=list)
    complete: bool = False


@dataclass
class ConditionRule:
    key: str
    scope: str
    text: str = ""


@dataclass
class MetalRate:
    code: str
    eur_per_kg: Decimal
    valid_from: date | None = None
    valid_to: date | None = None


@dataclass
class TechnicalRelease:
    key: str
    revision: str
    reserve_mm_per_end: Decimal


@dataclass
class PackagingTariff:
    key: str
    eur_per_carton: Decimal


@dataclass
class DeliveryCommitment:
    key: str
    material: str
    max_pieces: int
    lead_time_weeks: int


@dataclass
class FxRate:
    key: str
    currency: str
    eur_per_unit: Decimal


@dataclass
class SetupPool:
    key: str
    fee_eur: Decimal
    allocation: str = "other"


@dataclass
class BonusBand:
    lower_eur: Decimal
    upper_eur: Decimal | None
    percent: Decimal


@dataclass
class BonusPlan:
    key: str
    bands: list[BonusBand] = field(default_factory=list)
    allocation: str = "other"


@dataclass
class Message:
    id: str
    sender: str
    date: datetime | None
    subject: str = ""
    reference: str | None = None
    kind: str = "other"
    discount_percent: Decimal | None = None
    min_order_value_eur: Decimal | None = None
    condition_rules: list[ConditionRule] = field(default_factory=list)
    metal_index_rates: list[MetalRate] = field(default_factory=list)
    technical_releases: list[TechnicalRelease] = field(default_factory=list)
    packaging_tariffs: list[PackagingTariff] = field(default_factory=list)
    delivery_commitments: list[DeliveryCommitment] = field(default_factory=list)
    fx_rates: list[FxRate] = field(default_factory=list)
    setup_pools: list[SetupPool] = field(default_factory=list)
    bonus_plans: list[BonusPlan] = field(default_factory=list)
    tan: str | None = None
    locator: str | None = None


@dataclass
class Case:
    tender_reference: str | None = None
    scenario_date: datetime | None = None
    date_only: bool = False
    bidder_mail_domain: str | None = None
    authorized_senders: dict[str, str] = field(default_factory=dict)


@dataclass
class Evidence:
    case: Case = field(default_factory=Case)
    positions: list[Position] = field(default_factory=list)
    qa: list[QA] = field(default_factory=list)
    revisions: list[Revision] = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    searches: list[Search] = field(default_factory=list)
    tan: str | None = None
    mail_complete: bool = False
    conflicts: list[str] = field(default_factory=list)

    def reference(self) -> str | None:
        return self.case.tender_reference

    def catalog_groups(self) -> list[str]:
        groups: list[str] = []
        for revision in self.revisions:
            if revision.marking != "current":
                continue
            for requirement in revision.requirements:
                if requirement.catalog_group and requirement.catalog_group not in groups:
                    groups.append(requirement.catalog_group)
        return groups

    def summary(self) -> str:
        case = self.case
        lines = [
            f"tender_reference: {case.tender_reference or 'unknown'}",
            f"scenario_date: {case.scenario_date.isoformat() if case.scenario_date else 'unknown'}",
            f"mailbox_domain: {case.bidder_mail_domain or 'unknown'}",
            f"positions: {', '.join(p.position for p in self.positions) or 'none'}",
            f"qa: {', '.join(a.number for a in self.qa) or 'none'}",
            f"revisions: " + ", ".join(f"{r.id}:{r.marking}" for r in self.revisions) or "none",
            f"catalog_groups: {', '.join(self.catalog_groups()) or 'none'}",
            f"materials: {', '.join(m.number for m in self.materials) or 'none'}",
            f"messages: {', '.join(m.id for m in self.messages) or 'none'}",
            f"tan: {'present' if self.tan else 'absent'}",
            f"mail_search_finished: {self.mail_complete}",
        ]
        return "\n".join(lines)[:1800]


def _keep(old, new):
    """Fill empty fields on `old` from `new`. A non-empty list or a set value replaces."""
    for item in fields(new):
        value = getattr(new, item.name)
        if item.name in {"locator"}:
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, Attributes):
            current = getattr(old, item.name)
            for attr in fields(value):
                piece = getattr(value, attr.name)
                if piece not in (None, ""):
                    setattr(current, attr.name, piece)
            continue
        current = getattr(old, item.name)
        if item.name == "unit_kind" and value == "other" and current not in (None, "", "other"):
            continue
        if item.name == "marking" and value == "unknown" and current not in (None, "", "unknown"):
            continue
        if isinstance(value, list) and current and _richer(current, value):
            continue
        setattr(old, item.name, value)


def _richer(current: list, new: list) -> bool:
    return len(current) > len(new)


def _attrs(data: dict | None) -> Attributes:
    data = data or {}
    return Attributes(
        bore_mm=dec(data.get("bore_mm")),
        stroke_mm=dec(data.get("stroke_mm")),
        pressure_bar=dec(data.get("pressure_bar")),
        mounting=clean(data.get("mounting")) or None,
        certification=clean(data.get("certification")) or None,
    )


def _items(data, key: str) -> list:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


class Ledger:
    """In-run store. Merge is synchronous so asyncio tasks can share it without a lock."""

    def __init__(self) -> None:
        self.ev = Evidence()

    def note_domain(self, domain: str | None) -> None:
        if domain and not self.ev.case.bidder_mail_domain:
            self.ev.case.bidder_mail_domain = domain.lower().strip()

    def merge(self, data: dict, locator: str | None = None) -> int:
        if not isinstance(data, dict):
            return 0
        touched = 0
        touched += self._merge_case(data.get("case") or {})
        for row in _items(data, "positions"):
            if self._merge_position(row, locator):
                touched += 1
        for row in _items(data, "qa"):
            if self._merge_qa(row, locator):
                touched += 1
        for row in _items(data, "revisions"):
            if self._merge_revision(row, locator):
                touched += 1
        for row in _items(data, "materials"):
            if self._merge_material(row, locator):
                touched += 1
        for row in _items(data, "messages"):
            if self._merge_message(row, locator):
                touched += 1
        for row in _items(data, "searches"):
            if self._merge_search(row):
                touched += 1
        tan = clean(data.get("tan")) if isinstance(data.get("tan"), str) else ""
        if tan:
            self.ev.tan = tan
            touched += 1
        return touched

    def _merge_case(self, data: dict) -> int:
        if not isinstance(data, dict):
            return 0
        case = self.ev.case
        touched = 0
        reference = clean(data.get("tender_reference")) or None
        if reference:
            if case.tender_reference and ref_key(case.tender_reference) != ref_key(reference):
                self.ev.conflicts.append(f"tender reference {case.tender_reference} vs {reference}")
            elif not case.tender_reference:
                case.tender_reference = reference
                touched += 1
        when, date_only = as_datetime(data.get("scenario_date"))
        if when is not None and case.scenario_date is None:
            case.scenario_date = when
            case.date_only = date_only
            touched += 1
        domain = clean(data.get("bidder_mail_domain")).lower().lstrip("@")
        if domain and not case.bidder_mail_domain:
            case.bidder_mail_domain = domain
            touched += 1
        senders = data.get("authorized_senders")
        if isinstance(senders, dict):
            for key, value in senders.items():
                local = clean(str(value)).lower()
                if local:
                    case.authorized_senders[str(key)] = local.split("@")[0]
                    touched += 1
        return touched

    def _merge_position(self, row: dict, locator: str | None) -> bool:
        if not isinstance(row, dict) or not clean(row.get("position")):
            return False
        quantity = dec(row.get("quantity"))
        if quantity is None:
            return False
        incoming = Position(
            position=clean(row.get("position")),
            quantity=quantity,
            unit_raw=clean(row.get("unit_raw")),
            unit_kind=unit_of(row.get("unit_raw"), row.get("unit_kind")),
            requirement_id=clean(row.get("requirement_id")) or None,
            locator=locator,
        )
        return _upsert(self.ev.positions, incoming, lambda item: item.position == incoming.position)

    def _merge_qa(self, row: dict, locator: str | None) -> bool:
        if not isinstance(row, dict) or not clean(str(row.get("number") or "")):
            return False
        when, _ = as_datetime(row.get("date"))
        changes = []
        for change in _items(row, "quantity_changes"):
            quantity = dec(change.get("quantity")) if isinstance(change, dict) else None
            if quantity is None or not clean(change.get("position")):
                continue
            changes.append(QuantityChange(clean(change.get("position")), quantity, unit_of(change.get("unit_raw"), change.get("unit_kind"))))
        amendments = []
        for amendment in _items(row, "amendments"):
            if not isinstance(amendment, dict):
                continue
            amendments.append(Amendment(
                clean(amendment.get("revision")),
                clean(amendment.get("requirement_id")),
                _attrs(amendment.get("attributes")),
            ))
        assignments = []
        for assignment in _items(row, "assignments"):
            if isinstance(assignment, dict) and clean(assignment.get("position")):
                assignments.append(Assignment(
                    position=clean(assignment.get("position")),
                    condition_group=clean(assignment.get("condition_group")) or None,
                    packaging_tariff=clean(assignment.get("packaging_tariff")) or None,
                    delivery_commitment=clean(assignment.get("delivery_commitment")) or None,
                    setup_pool=clean(assignment.get("setup_pool")) or None,
                    bonus_plan=clean(assignment.get("bonus_plan")) or None,
                    setup_fee_eur=dec(assignment.get("setup_fee_eur")),
                ))
        incoming = QA(
            number=clean(str(row.get("number"))),
            date=when,
            reference=clean(row.get("reference")) or None,
            text=clean(row.get("text")),
            withdraws=[clean(str(item)) for item in _items(row, "withdraws") if clean(str(item))],
            quantity_changes=changes,
            amendments=amendments,
            assignments=assignments,
            locator=locator,
        )
        return _upsert(self.ev.qa, incoming, lambda item: item.number == incoming.number)

    def _merge_revision(self, row: dict, locator: str | None) -> bool:
        if not isinstance(row, dict) or not clean(row.get("id")):
            return False
        requirements = []
        for requirement in _items(row, "requirements"):
            if not isinstance(requirement, dict) or not clean(requirement.get("id")):
                continue
            raw = dict(requirement)
            if isinstance(requirement.get("attributes"), dict):
                raw.update(requirement["attributes"])
            requirements.append(Requirement(
                id=clean(requirement.get("id")),
                catalog_group=clean(requirement.get("catalog_group")) or None,
                usable_stroke=_flag(requirement.get("usable_stroke")),
                end_reserve_key=clean(requirement.get("end_reserve_key")) or None,
                pieces_per_set=as_int(requirement.get("pieces_per_set")),
                attributes=_attrs(raw),
            ))
        incoming = Revision(
            id=clean(row.get("id")),
            marking=marking_of(row.get("marking")),
            pieces_per_set=as_int(row.get("pieces_per_set")),
            requirements=requirements,
            locator=locator,
        )
        return _upsert(self.ev.revisions, incoming, lambda item: item.id == incoming.id)

    def _merge_material(self, row: dict, locator: str | None) -> bool:
        if not isinstance(row, dict) or not clean(row.get("number")):
            return False
        conditions = []
        for condition in _items(row, "price_conditions"):
            if not isinstance(condition, dict) or as_date(condition.get("valid_from")) is None:
                continue
            scales = []
            for scale in _items(condition, "scales"):
                if not isinstance(scale, dict) or dec(scale.get("price")) is None or as_int(scale.get("min_quantity")) is None:
                    continue
                scales.append(Scale(as_int(scale.get("min_quantity")), dec(scale.get("price"))))
            conditions.append(Condition(
                valid_from=as_date(condition.get("valid_from")),
                valid_to=as_date(condition.get("valid_to")),
                currency=clean(condition.get("currency") or "EUR").upper() or "EUR",
                fx_key=clean(condition.get("fx_key")) or None,
                price_unit=as_int(condition.get("price_unit")) or 1,
                scales=scales,
            ))
        raw_attributes = dict(row.get("attributes") or {})
        for key in ("bore_mm", "stroke_mm", "pressure_bar", "mounting", "certification"):
            if row.get(key) not in (None, ""):
                raw_attributes.setdefault(key, row.get(key))
        incoming = Material(
            number=clean(row.get("number")),
            status=status_of(row.get("status")),
            catalog_group=clean(row.get("catalog_group")) or None,
            attributes=_attrs(raw_attributes),
            stock_pieces=as_int(row.get("stock_pieces")),
            stock_lead_time_weeks=as_int(row.get("stock_lead_time_weeks")),
            production_lead_time_weeks=as_int(row.get("production_lead_time_weeks")),
            pieces_per_carton=as_int(row.get("pieces_per_carton")),
            allocation_mass_g=dec(row.get("allocation_mass_g")),
            surcharge_mass_kg=dec(row.get("surcharge_mass_kg")),
            metal_index=clean(row.get("metal_index")) or None,
            price_conditions=conditions,
            locator=locator,
        )
        return _upsert(self.ev.materials, incoming, lambda item: item.number == incoming.number)

    def _merge_message(self, row: dict, locator: str | None) -> bool:
        if not isinstance(row, dict) or not clean(str(row.get("id") or "")):
            return False
        when, _ = as_datetime(row.get("date"))
        incoming = Message(
            id=clean(str(row.get("id"))),
            sender=clean(row.get("sender")),
            date=when,
            subject=clean(row.get("subject")),
            reference=clean(row.get("reference")) or None,
            kind=_enum(row.get("kind"), KINDS, "other"),
            discount_percent=dec(row.get("discount_percent")),
            min_order_value_eur=dec(row.get("min_order_value_eur")),
            condition_rules=_rules(row),
            metal_index_rates=_rates(row),
            technical_releases=_releases(row),
            packaging_tariffs=_tariffs(row),
            delivery_commitments=_commitments(row),
            fx_rates=_fx(row),
            setup_pools=_pools(row),
            bonus_plans=_plans(row),
            tan=clean(row.get("tan")) or None,
            locator=locator,
        )
        if incoming.tan:
            self.ev.tan = incoming.tan
        return _upsert(self.ev.messages, incoming, lambda item: item.id == incoming.id)

    def _merge_search(self, row: dict) -> bool:
        if not isinstance(row, dict) or not clean(row.get("catalog_group")):
            return False
        incoming = Search(
            catalog_group=clean(row.get("catalog_group")),
            material_numbers=[clean(str(item)) for item in _items(row, "material_numbers") if clean(str(item))],
            complete=bool(row.get("complete")),
        )
        current = next((item for item in self.ev.searches if ref_key(item.catalog_group) == ref_key(incoming.catalog_group)), None)
        if current is None:
            self.ev.searches.append(incoming)
            return True
        current.material_numbers = sorted(set(current.material_numbers) | set(incoming.material_numbers))
        current.complete = current.complete or incoming.complete
        return True


def _upsert(bucket: list, incoming, same) -> bool:
    current = next((item for item in bucket if same(item)), None)
    if current is None:
        bucket.append(incoming)
        return True
    _keep(current, incoming)
    return True


def _rules(row: dict) -> list[ConditionRule]:
    rules = []
    for item in _items(row, "condition_rules"):
        if not isinstance(item, dict) or not clean(item.get("key")):
            continue
        scope = _enum(item.get("scope"), SCOPES, "")
        if scope not in SCOPES:
            continue
        rules.append(ConditionRule(clean(item.get("key")), scope, clean(item.get("text"))))
    return rules


def _rates(row: dict) -> list[MetalRate]:
    rates = []
    for item in _items(row, "metal_index_rates"):
        if isinstance(item, dict) and clean(item.get("code")) and dec(item.get("eur_per_kg")) is not None:
            rates.append(MetalRate(clean(item.get("code")), dec(item.get("eur_per_kg")), as_date(item.get("valid_from")), as_date(item.get("valid_to"))))
    return rates


def _releases(row: dict) -> list[TechnicalRelease]:
    found = []
    for item in _items(row, "technical_releases"):
        if isinstance(item, dict) and clean(item.get("key")) and dec(item.get("reserve_mm_per_end")) is not None:
            found.append(TechnicalRelease(clean(item.get("key")), clean(item.get("revision")), dec(item.get("reserve_mm_per_end"))))
    return found


def _tariffs(row: dict) -> list[PackagingTariff]:
    found = []
    for item in _items(row, "packaging_tariffs"):
        if isinstance(item, dict) and clean(item.get("key")) and dec(item.get("eur_per_carton")) is not None:
            found.append(PackagingTariff(clean(item.get("key")), dec(item.get("eur_per_carton"))))
    return found


def _commitments(row: dict) -> list[DeliveryCommitment]:
    found = []
    for item in _items(row, "delivery_commitments"):
        pieces = as_int(item.get("max_pieces")) if isinstance(item, dict) else None
        weeks = as_int(item.get("lead_time_weeks")) if isinstance(item, dict) else None
        if isinstance(item, dict) and clean(item.get("key")) and clean(item.get("material")) and pieces is not None and weeks is not None:
            found.append(DeliveryCommitment(clean(item.get("key")), clean(item.get("material")), pieces, weeks))
    return found


def _fx(row: dict) -> list[FxRate]:
    found = []
    for item in _items(row, "fx_rates"):
        if isinstance(item, dict) and clean(item.get("key")) and dec(item.get("eur_per_unit")) is not None:
            found.append(FxRate(clean(item.get("key")), clean(item.get("currency")).upper(), dec(item.get("eur_per_unit"))))
    return found


def _pools(row: dict) -> list[SetupPool]:
    found = []
    for item in _items(row, "setup_pools"):
        if isinstance(item, dict) and clean(item.get("key")) and dec(item.get("fee_eur")) is not None:
            found.append(SetupPool(clean(item.get("key")), dec(item.get("fee_eur")), _enum(item.get("allocation"), ALLOCATIONS, "other")))
    return found


def _plans(row: dict) -> list[BonusPlan]:
    found = []
    for item in _items(row, "bonus_plans"):
        if not isinstance(item, dict) or not clean(item.get("key")):
            continue
        bands = []
        for band in _items(item, "bands"):
            if isinstance(band, dict) and dec(band.get("lower_eur")) is not None and dec(band.get("percent")) is not None:
                bands.append(BonusBand(dec(band.get("lower_eur")), dec(band.get("upper_eur")), dec(band.get("percent"))))
        found.append(BonusPlan(clean(item.get("key")), bands, _enum(item.get("allocation"), ALLOCATIONS, "other")))
    return found


