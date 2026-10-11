"""The bid arithmetic: resolved facts in, the answer's numbers out. Pure functions, no browser, no model.

Every input here has already been chosen by the site agents and checked by the resolver: the final quantity, the
matched material, the valid price condition, the governing approval and so on. This module only applies the brief's
calculation rules to them, in Decimal, rounding HALF_UP to cents exactly where the brief says.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")

# Which parts of the unit price a Konditionsgruppe lets the order discount reduce. Setup fees never.
SCOPE_BASE = "base"
SCOPE_BASE_AND_SURCHARGE = "base_and_surcharge"
SCOPE_NONE = "none"
SCOPES = (SCOPE_BASE, SCOPE_BASE_AND_SURCHARGE, SCOPE_NONE)


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --- inputs -------------------------------------------------------------------------------------------------------

@dataclass
class PriceScale:
    min_quantity: int
    price: Decimal  # in the condition's currency, per `price_unit` pieces


@dataclass
class PriceCondition:
    scales: list[PriceScale]
    currency: str = "EUR"
    price_unit: int = 1             # pieces the scale price is quoted for (e.g. per 100)
    eur_per_unit: Decimal = Decimal(1)  # EUR per one unit of `currency`, from the Devisenfixing


@dataclass
class Commitment:
    """A Terminfreigabe delivery commitment assigned to a position by the tender Q&A."""
    material: str
    ceiling: int        # pooled final pieces of that material, inclusive
    lead_time_weeks: int


@dataclass
class Line:
    position: str
    quantity: int                         # final pieces
    material: str | None                  # None: no active matching material, so the whole bid is no_bid
    condition: PriceCondition | None = None
    stock: int = 0                        # pieces on stock of this material (shared by its positions)
    stock_lead_time_weeks: int = 0
    production_lead_time_weeks: int = 0
    commitment: Commitment | None = None
    mass_surcharge_per_unit: Decimal = ZERO   # EUR per piece, unrounded, before any discount
    setup_fee: Decimal = ZERO                 # ordinary setup fee for the position, EUR
    discount_scope: str = SCOPE_BASE          # from the position's Konditionsgruppe; default base only
    carton_size: int | None = None            # pieces per carton (ERP)
    carton_price: Decimal = ZERO              # EUR per carton (Verpackungstarif)
    setup_pool: bool = False                  # member of the shared setup pool (Rüstpool)
    allocation_mass: Decimal = ZERO           # ERP allocation mass per piece (not the surcharge mass)
    bonus_eligible: bool = False              # member of the project bonus


@dataclass
class Approval:
    percent: Decimal
    min_order_value: Decimal | None = None   # inclusive threshold on the undiscounted net order value


@dataclass
class BonusBand:
    lower: Decimal              # EUR, the band applies to the part of the total above this ...
    upper: Decimal | None       # ... up to this (None: no upper bound)
    percent: Decimal


@dataclass
class Order:
    tender_reference: str
    lines: list[Line]
    approval: Approval | None = None
    setup_pool_fee: Decimal = ZERO
    bonus_bands: list[BonusBand] = field(default_factory=list)


# --- the rules ----------------------------------------------------------------------------------------------------

def scale_price(condition: PriceCondition, quantity: int) -> Decimal:
    """The scale with the highest minimum quantity no greater than the quantity, normalized to one piece and
    converted to EUR, HALF_UP to cents. This is base_unit_price; everything after uses it."""
    eligible = [s for s in condition.scales if s.min_quantity <= quantity]
    if not eligible:
        raise ValueError(f"no price scale reaches quantity {quantity}")
    scale = max(eligible, key=lambda s: s.min_quantity)
    per_piece = D(scale.price) / D(condition.price_unit)
    return cents(per_piece * D(condition.eur_per_unit))


def discount_percent(order: Order, base_prices: dict[str, Decimal]) -> Decimal:
    """The approved percentage, or 0 if its minimum order value (undiscounted goods, no surcharges or setup,
    rounded to cents, inclusive) is not reached. No fallback to older approvals: the resolver passes only the latest."""
    if order.approval is None:
        return ZERO
    if order.approval.min_order_value is not None:
        value = cents(sum((line.quantity * base_prices[line.position] for line in order.lines), ZERO))
        if value < D(order.approval.min_order_value):
            return ZERO
    return D(order.approval.percent)


def pooled_demand(lines: list[Line]) -> dict[str, int]:
    demand: dict[str, int] = {}
    for line in lines:
        demand[line.material] = demand.get(line.material, 0) + line.quantity
    return demand


def lead_time(line: Line, demand: dict[str, int]) -> int:
    """A matching commitment within its ceiling wins; otherwise stock delivery only if stock covers the pooled
    demand of every position on that material, else production."""
    pooled = demand[line.material]
    c = line.commitment
    if c is not None and c.material == line.material and pooled <= c.ceiling:
        return c.lead_time_weeks
    return line.stock_lead_time_weeks if line.stock >= pooled else line.production_lead_time_weeks


def packaging_cost(line: Line) -> Decimal:
    if not line.carton_size:
        return ZERO
    return math.ceil(line.quantity / line.carton_size) * D(line.carton_price)


def allocate_cents(total: Decimal, weights: dict[str, Decimal]) -> dict[str, Decimal]:
    """Split `total` (whole cents) proportionally to `weights` by largest remainders; ties go to the ascending
    position identifier."""
    total_cents = int((D(total) / CENT).to_integral_value(rounding=ROUND_HALF_UP))
    weight_sum = sum(weights.values(), ZERO)
    if total_cents == 0 or weight_sum == 0:
        return {key: ZERO for key in weights}
    exact = {key: Decimal(total_cents) * w / weight_sum for key, w in weights.items()}
    floors = {key: int(v) for key, v in exact.items()}
    left = total_cents - sum(floors.values())
    by_remainder = sorted(weights, key=lambda key: (-(exact[key] - floors[key]), _position_key(key)))
    for key in by_remainder[:left]:
        floors[key] += 1
    return {key: Decimal(c) * CENT for key, c in floors.items()}


def _position_key(position: str):
    """Ascending position identifier: numeric parts compared as numbers ("2" before "10", "1.2" before "1.10")."""
    # (0, number) or (1, text) per part, so identifiers that mix letters and digits ("A.1", "1.1a") still compare
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                 for part in position.replace("-", ".").split("."))


def project_bonus(bands: list[BonusBand], eligible_total: Decimal) -> Decimal:
    """Marginal bands: each percentage applies only to the part of the total inside its band. HALF_UP to cents."""
    credit = ZERO
    for band in bands:
        upper = eligible_total if band.upper is None else min(eligible_total, D(band.upper))
        portion = upper - D(band.lower)
        if portion > 0:
            credit += portion * D(band.percent) / 100
    return cents(credit)


@dataclass
class PricedLine:
    position: str
    material_number: str
    quantity: int
    unit_price: Decimal
    lead_time_weeks: int
    base_unit_price: Decimal
    surcharge_per_unit: Decimal
    packaging_per_unit: Decimal
    rebate_total: Decimal


def no_bid(tender_reference: str) -> dict:
    """No positions, a total of zero, and 0 % (a conditional approval yields 0 % for no_bid)."""
    return {"tender_reference": tender_reference, "decision": "no_bid", "currency": "EUR",
            "discount_percent": 0, "positions": [], "total_net": 0}


def price_order(order: Order) -> dict:
    """The answer: a bid with one entry per position, or no_bid when any position lacks an active material."""
    if any(line.material is None for line in order.lines):
        return no_bid(order.tender_reference)
    for line in order.lines:
        if line.discount_scope not in SCOPES:
            raise ValueError(f"position {line.position}: unknown discount scope {line.discount_scope!r}")
        if line.quantity <= 0:
            raise ValueError(f"position {line.position}: quantity {line.quantity}")

    base = {line.position: scale_price(line.condition, line.quantity) for line in order.lines}
    pct = discount_percent(order, base)
    factor = 1 - pct / 100
    demand = pooled_demand(order.lines)

    pool = allocate_cents(order.setup_pool_fee, {
        line.position: line.quantity * D(line.allocation_mass) for line in order.lines if line.setup_pool})
    goods = {line.position: line.quantity * base[line.position] for line in order.lines if line.bonus_eligible}
    bonus = allocate_cents(project_bonus(order.bonus_bands, sum(goods.values(), ZERO)), goods) if goods else {}

    priced = []
    for line in order.lines:
        q = Decimal(line.quantity)
        surcharge = D(line.mass_surcharge_per_unit)
        setup = D(line.setup_fee) + pool.get(line.position, ZERO)
        packaging = packaging_cost(line)
        rebate = bonus.get(line.position, ZERO)
        discounted_base = base[line.position] * (factor if line.discount_scope != SCOPE_NONE else 1)
        discounted_surcharge = surcharge * (factor if line.discount_scope == SCOPE_BASE_AND_SURCHARGE else 1)
        unit_price = cents(discounted_base + discounted_surcharge + (setup + packaging - rebate) / q)
        priced.append(PricedLine(
            position=line.position,
            material_number=line.material,
            quantity=line.quantity,
            unit_price=unit_price,
            lead_time_weeks=lead_time(line, demand),
            base_unit_price=base[line.position],
            surcharge_per_unit=cents(surcharge + setup / q),
            packaging_per_unit=cents(packaging / q),
            rebate_total=rebate,
        ))

    total = cents(sum((p.quantity * p.unit_price for p in priced), ZERO))
    return {
        "tender_reference": order.tender_reference,
        "decision": "bid",
        "currency": "EUR",
        "discount_percent": float(pct),
        "positions": [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in vars(p).items()} for p in priced],
        "total_net": float(total),
    }
