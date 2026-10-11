"""The evidence: everything the research agents found, as typed records. The one contract between retrieval and code.

Agents record evidence, not conclusions: every revision with its marking, every Q&A answer, every director mail
(also the ones that look like decoys), every ERP candidate of a searched Kataloggruppe. resolver.py applies the brief's
rules to these records (exact reference, authorized sender, no later than the scenario date, latest wins, withdrawals,
amendments, ...) and pricing.py does the arithmetic.

Records are grouped by what they are, not by the website they came from, so an agent on any site can add any record
(the private dataset splits the information differently). Merge the parts of several agents with `merge`.

The field descriptions are written for the extracting model: `Evidence.model_json_schema()` is the save_facts schema.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# A time without a zone is taken as UTC, so every comparison with the scenario date works.
When = Annotated[datetime, AfterValidator(lambda v: v if v.tzinfo else v.replace(tzinfo=timezone.utc))]
UnitKind = Literal["piece", "set", "other"]
Scope = Literal["base", "base_and_surcharge", "none"]
MessageKind = Literal[
    "discount_approval", "condition_rules", "metal_index", "technical_release", "packaging_tariffs",
    "delivery_commitments", "fx_fixing", "setup_pools", "project_bonus", "signing_tan", "other"]


class Record(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Source(Record):
    site: str = Field(description="Site id the record was read on, e.g. tender-portal, erp, mail")
    locator: str | None = Field(None, description="URL, document name, message id or page where it was read")


# --- the case ---------------------------------------------------------------------------------------------------

class Case(Record):
    tender_reference: str = Field(description="The exact tender reference from the brief")
    scenario_date: When = Field(description="The scenario date from the brief, ISO 8601 with timezone")
    bidder_mail_domain: str = Field(description="Domain of the bidder's own mailbox, e.g. example.com")
    authorized_senders: dict[str, str] | None = Field(
        None, description="Only if the brief names different ones: message kind -> sender local part (before @)")


# --- tender ------------------------------------------------------------------------------------------------------

class TenderPosition(Record):
    position: str = Field(description="Position number exactly as in the bill of quantities")
    quantity: Decimal = Field(description="Original tender quantity in the position's own unit")
    unit_raw: str = Field(description="The unit as shown, e.g. Stk, Stück, Satz")
    unit_kind: UnitKind = Field(description="piece for Stk/Stück/pcs, set for Satz")
    requirement_id: str | None = Field(None, description="The specification requirement the position refers to")
    source: Source | None = None


class QuantityChange(Record):
    position: str
    quantity: Decimal
    unit_kind: UnitKind


class RequirementAttributes(Record):
    """Technical attributes as numbers and plain text. Omit what is not stated."""
    bore_mm: Decimal | None = None
    stroke_mm: Decimal | None = Field(None, description="Stroke (Hub/Stellweg) as stated, in mm")
    pressure_bar: Decimal | None = None
    mounting: str | None = Field(None, description="Befestigung as written, e.g. Schwenkauge")
    certification: str | None = Field(None, description="As written; 'none' when none is required")


class Amendment(Record):
    revision: str = Field(description="The specification revision the amendment names")
    requirement_id: str
    attributes: RequirementAttributes = Field(description="Only the attributes the amendment replaces")


class Assignment(Record):
    """Keys a Q&A answer assigns to one position. Leave a field null when the answer does not assign it."""
    position: str
    condition_group: str | None = Field(None, description="Konditionsgruppe key")
    packaging_tariff: str | None = Field(None, description="Verpackungstarif key")
    delivery_commitment: str | None = Field(None, description="Terminschlüssel / Terminfreigabe key")
    setup_pool: str | None = Field(None, description="Rüstpool key")
    bonus_plan: str | None = Field(None, description="Projektbonus / Bonusplan key")
    setup_fee_eur: Decimal | None = Field(None, description="Ordinary one-off setup fee (Rüstkosten) in EUR net")


class QAAnswer(Record):
    number: str = Field(description="The answer's number as shown")
    date: When = Field(description="Publication date, ISO 8601 with timezone (apply the site's format hints)")
    reference: str | None = Field(None, description="The tender reference the answer is for, if shown")
    text: str = Field(description="The answer verbatim. Code never reads it: every quantity change, technical "
                                  "amendment, withdrawal and key assignment it states goes into the fields below")
    withdraws: list[str] = Field(default_factory=list, description="Numbers of earlier answers it withdraws")
    quantity_changes: list[QuantityChange] = Field(default_factory=list)
    amendments: list[Amendment] = Field(default_factory=list, description="Technical amendments (Nachträge)")
    assignments: list[Assignment] = Field(default_factory=list)
    source: Source | None = None


# --- specification -------------------------------------------------------------------------------------------------

class Requirement(RequirementAttributes):
    id: str = Field(description="Requirement id, e.g. as referenced by the tender positions")
    catalog_group: str | None = Field(None, description="Kataloggruppe to search in the ERP")
    usable_stroke: bool = Field(False, description="True when the stroke is the usable travel (nutzbarer Weg)")
    end_reserve_key: str | None = Field(None, description="Endreserve key of a usable-stroke requirement")
    pieces_per_set: int | None = Field(None, description="Pieces per Satz, when the specification states it")


class SpecRevision(Record):
    id: str
    document: str | None = Field(None, description="Specification document number")
    date: When | None = None
    marking: Literal["current", "superseded", "unknown"] = Field(
        description="current only when the revision itself is marked current/gültig; never inferred from its date")
    pieces_per_set: int | None = Field(None, description="Pieces per Satz, when stated for the whole document")
    requirements: list[Requirement] = Field(default_factory=list)
    source: Source | None = None


# --- ERP ---------------------------------------------------------------------------------------------------------

class PriceScale(Record):
    min_quantity: int = Field(description="Minimum quantity of the scale, in pieces")
    price: Decimal = Field(description="Scale price in the condition's currency per price unit")


class PriceCondition(Record):
    valid_from: date
    valid_to: date | None = Field(None, description="Last valid day; null when open-ended")
    currency: str = "EUR"
    fx_key: str | None = Field(None, description="FX key named by the condition, for a non-EUR currency")
    price_unit: int = Field(1, description="Pieces the price is quoted for, e.g. 100 for 'je 100 Stück'")
    scales: list[PriceScale]


class StockRow(Record):
    category: str = Field(description="The stock category exactly as the system labels it")
    kind: Literal["free", "reserved", "blocked", "quality_inspection", "in_transit", "total", "other"] = Field(
        description="free: unrestricted, available for delivery now; total: a sum over several categories")
    quantity: int


class Material(Record):
    number: str
    status: Literal["active", "blocked", "other"]
    status_raw: str | None = Field(None, description="The status code or text as shown")
    catalog_group: str | None = None
    attributes: RequirementAttributes
    stock_rows: list[StockRow] = Field(default_factory=list, description=(
        "Every stock figure the system shows for this material, one row per category or column (also totals and "
        "figures that are reserved, blocked, in inspection or in transit). Record all of them as shown; code uses "
        "only the freely available stock."))
    stock_pieces: int | None = Field(None, description="Only when the system shows one single stock figure without "
                                                       "any categories")
    stock_lead_time_weeks: int | None = None
    production_lead_time_weeks: int | None = None
    pieces_per_carton: int | None = Field(None, description="Packinhalt")
    allocation_mass_g: Decimal | None = Field(None, description="Umlagegewicht per piece, in grams")
    surcharge_mass_kg: Decimal | None = Field(None, description="Mass for the metal surcharge per piece, in kg")
    metal_index: str | None = Field(None, description="Metal index code the surcharge uses")
    price_conditions: list[PriceCondition] = Field(default_factory=list)
    source: Source | None = None


class ErpSearch(Record):
    catalog_group: str
    material_numbers: list[str] = Field(description="Every material the narrowed search listed")
    complete: bool = Field(description="True when every listed candidate was opened and recorded")


# --- mail --------------------------------------------------------------------------------------------------------

class ConditionRule(Record):
    key: str
    scope: Scope = Field(description="What the order discount reduces: base price only, base and mass surcharge, "
                                     "or nothing. Setup fees are never discounted.")
    text: str = Field(description="The rule verbatim")


class MetalIndexRate(Record):
    code: str
    eur_per_kg: Decimal
    valid_from: date | None = None
    valid_to: date | None = None


class TechnicalRelease(Record):
    key: str = Field(description="Endreserve key")
    revision: str
    reserve_mm_per_end: Decimal


class PackagingTariff(Record):
    key: str
    eur_per_carton: Decimal


class DeliveryCommitment(Record):
    key: str
    material: str
    max_pieces: int = Field(description="Inclusive ceiling on the pooled demand of that material")
    lead_time_weeks: int


class FxRate(Record):
    key: str
    currency: str
    eur_per_unit: Decimal = Field(description="EUR for one unit of the currency")


class SetupPool(Record):
    key: str
    fee_eur: Decimal
    allocation: Literal["floor_largest_remainder_ascending_position", "other"] = Field(
        description="The cent-allocation rule the mail states")


class BonusBand(Record):
    lower_eur: Decimal
    upper_eur: Decimal | None = None
    percent: Decimal


class BonusPlan(Record):
    key: str
    bands: list[BonusBand]
    allocation: Literal["floor_largest_remainder_ascending_position", "other"]


class Message(Record):
    id: str
    folder: str | None = None
    sender: str = Field(description="Full sender address")
    date: When = Field(description="ISO 8601 with timezone (apply the site's format hints)")
    subject: str
    reference: str | None = Field(description="The tender reference the message is for, exactly as written")
    kind: MessageKind = Field(description="What the message is. A request, a review or a reminder is 'other'")
    discount_percent: Decimal | None = Field(None, description="discount_approval: the approved percentage")
    min_order_value_eur: Decimal | None = Field(
        None, description="discount_approval: minimum undiscounted net order value, if the approval sets one")
    condition_rules: list[ConditionRule] = Field(default_factory=list)
    metal_index_rates: list[MetalIndexRate] = Field(default_factory=list)
    technical_releases: list[TechnicalRelease] = Field(default_factory=list)
    packaging_tariffs: list[PackagingTariff] = Field(default_factory=list)
    delivery_commitments: list[DeliveryCommitment] = Field(default_factory=list)
    fx_rates: list[FxRate] = Field(default_factory=list)
    setup_pools: list[SetupPool] = Field(default_factory=list)
    bonus_plans: list[BonusPlan] = Field(default_factory=list)
    source: Source | None = None


# --- the agents' own reading, checked by the resolver ---------------------------------------------------------------

class AgentPicks(Record):
    """What the agents themselves concluded. The resolver compares and reports any disagreement."""
    governing_revision: str | None = None
    discount_message_id: str | None = None
    materials: dict[str, str | None] = Field(default_factory=dict, description="position -> material or null")
    final_quantities: dict[str, int] = Field(default_factory=dict, description="position -> final pieces")


class Evidence(Record):
    case: Case
    positions: list[TenderPosition] = Field(default_factory=list)
    qa: list[QAAnswer] = Field(default_factory=list)
    revisions: list[SpecRevision] = Field(default_factory=list)
    materials: list[Material] = Field(default_factory=list)
    erp_searches: list[ErpSearch] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    picks: AgentPicks = Field(default_factory=AgentPicks)
    notes: list[str] = Field(default_factory=list)


_KEYS = {"positions": "position", "qa": "number", "revisions": "id", "materials": "number",
         "erp_searches": "catalog_group", "messages": "id"}


def merge(case: Case, *parts: dict | Evidence) -> Evidence:
    """One Evidence from several agents' parts. A record with the same id as an earlier one replaces it."""
    lists: dict[str, dict[str, Record]] = {name: {} for name in _KEYS}
    picks, notes = AgentPicks(), []
    for part in parts:
        part = part if isinstance(part, Evidence) else Evidence.model_validate({"case": case, **part})
        for name, key in _KEYS.items():
            for record in getattr(part, name):
                lists[name][str(getattr(record, key))] = record
        picks = picks.model_copy(update={k: v for k, v in part.picks.model_dump().items() if v})
        notes += part.notes
    return Evidence(case=case, picks=picks, notes=notes, **{name: list(d.values()) for name, d in lists.items()})
