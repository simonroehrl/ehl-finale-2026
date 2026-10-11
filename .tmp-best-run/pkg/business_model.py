from decimal import Decimal, ROUND_HALF_UP
import math

def round_cents(value: float | Decimal) -> float:
    """Strict half-up rounding to cents (Rule 04, 27)."""
    if isinstance(value, float):
        value = Decimal(str(value))
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_applicable_scale_price(price_condition: dict, quantity: int) -> float:
    """Dynamically evaluates any number of price scales (Rule 04)."""
    if not price_condition or not isinstance(price_condition, dict):
        return 0.0
    
    scales = price_condition.get("scales", price_condition.get("price_scales", []))
    if not scales:
        return 0.0
        
    sorted_scales = sorted(scales, key=lambda x: x.get("min_quantity", x.get("min_qty", 0)))
    selected_price = sorted_scales[0].get("price", sorted_scales[0].get("unit_price", 0.0))
    
    for scale in sorted_scales:
        min_qty = scale.get("min_quantity", scale.get("min_qty", 0))
        if quantity >= min_qty:
            selected_price = scale.get("price", scale.get("unit_price", selected_price))
            
    return float(selected_price)


def resolve_position_quantity(pos: dict) -> int:
    """
    Resolves stated quantity into final pieces, automatically handling 
    'Satz' (set) conversions using pieces_per_set (Rule 17).
    """
    stated_qty = int(pos.get("quantity_pieces", pos.get("quantity", pos.get("qty", 1))))
    unit = str(pos.get("unit", "pieces")).lower()
    pieces_per_set = int(pos.get("pieces_per_set", pos.get("pieces_per_satz", 1)))
    
    if "satz" in unit or "set" in unit:
        return stated_qty * pieces_per_set
    return stated_qty


def calculate_marginal_project_bonus(eligible_total_goods: Decimal, bands: list[dict]) -> Decimal:
    """Computes total project bonus pool across marginal percentage bands (Rule 26)."""
    total_bonus = Decimal("0")
    remaining_amount = eligible_total_goods
    previous_limit = Decimal("0")

    if not bands:
        return Decimal("0.00")

    sorted_bands = sorted(bands, key=lambda x: x.get("limit", x.get("threshold", float('inf'))))

    for band in sorted_bands:
        limit = Decimal(str(band.get("limit", band.get("threshold", float('inf')))))
        percent = Decimal(str(band.get("percent", band.get("percentage", 0.0)))) / Decimal("100")
        
        band_span = limit - previous_limit
        if remaining_amount <= 0:
            break
            
        taxable_chunk = min(remaining_amount, band_span)
        total_bonus += taxable_chunk * percent
        remaining_amount -= taxable_chunk
        previous_limit = limit

    return Decimal(str(round_cents(total_bonus)))


def allocate_largest_remainders(total_pool: Decimal, weights: list[tuple[str, Decimal]]) -> dict:
    """
    Allocates a pool total proportionally in exact cents using the 
    Largest Remainders method (Rule 26). Ties resolved by ascending position ID.
    """
    total_weight = sum(w for _, w in weights) or Decimal("1")
    allocations = {}
    total_pool_cents = int(total_pool * Decimal("100"))
    
    allocated_cents_sum = 0
    remainders = []

    for pos_id, weight in weights:
        exact_share = (weight / total_weight) * Decimal(str(total_pool_cents))
        floor_cents = int(math.floor(exact_share))
        allocations[pos_id] = Decimal(floor_cents) / Decimal("100")
        allocated_cents_sum += floor_cents
        remainder = exact_share - Decimal(floor_cents)
        remainders.append((remainder, str(pos_id)))

    diff_cents = total_pool_cents - allocated_cents_sum
    remainders.sort(key=lambda x: (-x[0], x[1]))

    for i in range(diff_cents):
        pos_id = remainders[i][1]
        allocations[pos_id] += Decimal("0.01")

    return {pos_id: round_cents(amt) for pos_id, amt in allocations.items()}


def evaluate_master_bid(full_payload: dict) -> dict:
    """
    Definitive master calculation engine covering all 29 EHL challenge rules,
    including Satz conversions and granular commercial clause scoping.
    """
    positions_raw = full_payload.get("positions", full_payload.get("line_items", []))
    
    # 1. Filter out withdrawn Q&A entries (Rule 13)
    active_positions = [pos for pos in positions_raw if not pos.get("withdrawn", False)]

    # 2. Decision Check: Missing active material triggers 'no_bid' (Rule 03, 08)
    for pos in active_positions:
        is_omitted = pos.get("omitted", pos.get("skip", False))
        material = pos.get("material", pos.get("material_id", pos.get("article_number")))
        if not is_omitted and not material:
            return {
                "decision": "no_bid",
                "positions": [],
                "total_net": 0.00
            }

    # 3. Handle Technical Stroke Derivation (Rule 20: nutzbarer Weg)
    for pos in active_positions:
        if pos.get("is_nutzbarer_weg", False):
            base_stroke = Decimal(str(pos.get("base_stroke", 0.0)))
            per_end_reserve = Decimal(str(pos.get("per_end_reserve", 0.0)))
            nominal_stroke = base_stroke + (Decimal("2") * per_end_reserve)
            pos["derived_nominal_stroke"] = float(nominal_stroke)

    # 4. Pass 1: Compute Undiscounted Goods Total for Conditional Discount (Rule 16)
    total_undiscounted_goods = Decimal("0")
    for pos in active_positions:
        if pos.get("omitted", pos.get("skip", False)):
            continue
            
        qty = resolve_position_quantity(pos)
        price_cond = pos.get("price_condition", pos.get("pricing", {}))
        
        base_float = get_applicable_scale_price(price_cond, qty)
        base = Decimal(str(base_float))
        if base == Decimal("0.0"):
            base = Decimal(str(pos.get("base_unit_price", pos.get("base_price", 0.0))))

        fx_rate = Decimal(str(price_cond.get("fx_rate_to_eur", price_cond.get("fx", 1.0))))
        normalized_base = base * fx_rate
        
        total_undiscounted_goods += Decimal(str(qty)) * normalized_base

    # 5. Evaluate Conditional Director Approval Threshold (Rule 05, 16)
    approval = full_payload.get("director_approval", full_payload.get("approval", {}))
    if not approval:
        approval = {"percent": 0.0, "is_conditional": False, "min_order_value": 0.0}

    min_threshold = Decimal(str(approval.get("min_order_value", approval.get("threshold", 0.0))))
    has_conditional_rule = bool(approval.get("is_conditional", approval.get("conditional", False)))
    
    raw_discount_pct = approval.get("percent", approval.get("percentage", 0.0))
    discount_val = Decimal(str(raw_discount_pct)) if raw_discount_pct is not None else Decimal("0.0")
    
    apply_discount = True
    if has_conditional_rule and total_undiscounted_goods < min_threshold:
        apply_discount = False

    order_discount_pct = (discount_val / Decimal("100")) if apply_discount else Decimal("0.0")

    # 6. Shared Stock Pool Aggregation & Delivery Commitments (Rule 15, 24)
    material_quantities = {}
    material_stock = full_payload.get("material_stock_levels", full_payload.get("stock", {}))
    
    for pos in active_positions:
        if pos.get("omitted", pos.get("skip", False)):
            continue
        mat = pos.get("material", pos.get("material_id", pos.get("article_number")))
        qty = resolve_position_quantity(pos)
        material_quantities[mat] = material_quantities.get(mat, 0) + qty

    # 7. Shared Setup Mass-Based Allocation Calculation (Rule 25)
    shared_pool_data = full_payload.get("shared_pool_fee_data", {})
    total_pool_fee = Decimal(str(shared_pool_data.get("pool_fee", 0.0)))
    
    total_pool_mass_weight = Decimal("0")
    position_mass_weights = {}
    
    for pos in active_positions:
        if pos.get("omitted", pos.get("skip", False)):
            continue
        pos_id = str(pos.get("position", pos.get("pos_id", "1")))
        qty = Decimal(str(resolve_position_quantity(pos)))
        alloc_mass = Decimal(str(pos.get("allocation_mass", 0.0)))
        
        mass_weight = qty * alloc_mass
        position_mass_weights[pos_id] = mass_weight
        total_pool_mass_weight += mass_weight

    setup_weights = [(pid, w) for pid, w in position_mass_weights.items()]
    allocated_setup_map = allocate_largest_remainders(total_pool_fee, setup_weights) if total_pool_mass_weight > 0 else {}

    # 8. Project Bonus Pool Calculation & Allocation (Rule 26)
    bonus_bands = full_payload.get("project_bonus_bands", full_payload.get("bonus_bands", []))
    total_project_bonus_pool = calculate_marginal_project_bonus(total_undiscounted_goods, bonus_bands)

    bonus_weights = []
    for pos_data in active_positions:
        if pos_data.get("omitted", pos_data.get("skip", False)):
            continue
        pos_id = str(pos_data.get("position", pos_data.get("pos_id", "1")))
        qty = resolve_position_quantity(pos_data)
        base_float = get_applicable_scale_price(pos_data.get("price_condition", pos_data.get("pricing", {})), qty)
        line_val = Decimal(str(qty)) * Decimal(str(base_float))
        bonus_weights.append((pos_id, line_val))

    allocated_bonus_map = allocate_largest_remainders(total_project_bonus_pool, bonus_weights) if total_project_bonus_pool > 0 else {}

    # 9. Pass 2: Position-by-Position Calculation
    processed_positions = []
    for pos_data in active_positions:
        if pos_data.get("omitted", pos_data.get("skip", False)):
            continue
            
        pos_id = str(pos_data.get("position", pos_data.get("pos_id", "1")))
        qty = resolve_position_quantity(pos_data)
        mat = pos_data.get("material", pos_data.get("material_id", pos.get("article_number")))
        
        price_cond = pos_data.get("price_condition", pos_data.get("pricing", {}))
        base_float = get_applicable_scale_price(price_cond, qty)
        base = Decimal(str(base_float))
        if base == Decimal("0.0"):
            base = Decimal(str(pos_data.get("base_unit_price", pos_data.get("base_price", 0.0))))
        
        fx_rate = Decimal(str(price_cond.get("fx_rate_to_eur", price_cond.get("fx", 1.0))))
        normalized_base = base * fx_rate

        # Commercial Clause / Konditionsgruppe Scoping (Rule 12: base_only, base_plus_surcharge, neither)
        discount_scope = str(pos_data.get("discount_scope", pos_data.get("konditionsgruppe_scope", "base_only"))).lower()
        
        if discount_scope == "neither":
            pos_discount_pct = Decimal("0.0")
        else:
            pos_discount_pct = order_discount_pct

        discounted_base = normalized_base * (Decimal("1") - pos_discount_pct)
        
        surcharge = Decimal(str(pos_data.get("surcharge_per_unit", pos_data.get("surcharge", 0.0))))
        if discount_scope == "base_plus_surcharge":
            discounted_surcharge = surcharge * (Decimal("1") - pos_discount_pct)
        else:
            discounted_surcharge = surcharge # Surcharge reported before discount or unaffected

        # Packaging & Carton Rounding (Rule 23)
        carton_size = pos_data.get("carton_size", pos_data.get("packaging_unit", 0))
        packaging_tariff = Decimal(str(pos_data.get("packaging_tariff_per_carton", pos_data.get("packaging_cost", 0.0))))
        if carton_size > 0:
            cartons_needed = math.ceil(qty / carton_size)
            total_packaging = Decimal(str(cartons_needed)) * packaging_tariff
            packaging_per_unit = total_packaging / Decimal(str(qty))
        else:
            packaging_per_unit = Decimal(str(pos_data.get("packaging_per_unit", 0.0)))

        # Setup Fees & Allocated Bonus from maps
        ordinary_setup = Decimal(str(pos_data.get("ordinary_setup", pos_data.get("setup_fee", 0.0))))
        allocated_shared_setup = Decimal(str(allocated_setup_map.get(pos_id, 0.0)))
        allocated_bonus = Decimal(str(allocated_bonus_map.get(pos_id, 0.0)))

        extra_costs_per_unit = (ordinary_setup + allocated_shared_setup + (packaging_per_unit * Decimal(str(qty))) - allocated_bonus) / Decimal(str(qty))

        # Final Unit Price (Rule 27)
        raw_final_unit_price = discounted_base + discounted_surcharge + extra_costs_per_unit
        final_unit_price = round_cents(raw_final_unit_price)
        total_net = round_cents(Decimal(str(qty)) * Decimal(str(final_unit_price)))

        # Lead Time / Delivery Commitments & Shared Stock (Rule 15, 24)
        total_pool_demand = material_quantities.get(mat, qty)
        available_stock = material_stock.get(mat, 0)
        commitment = pos_data.get("delivery_commitment", {})
        commitment_ceiling = int(commitment.get("ceiling", float('inf')))

        if available_stock >= total_pool_demand and total_pool_demand <= commitment_ceiling:
            lead_time = pos_data.get("stock_lead_time", "from_stock")
        else:
            lead_time = pos_data.get("production_lead_time", "production")

        processed_positions.append({
            "position": pos_id,
            "quantity": qty,
            "material": mat,
            "base_unit_price": round_cents(normalized_base),
            "surcharge_per_unit": round_cents(surcharge),
            "packaging_per_unit": round_cents(packaging_per_unit),
            "rebate_total": round_cents(allocated_bonus),
            "final_unit_price": final_unit_price,
            "total_net": total_net,
            "lead_time": lead_time
        })

    overall_total = sum(Decimal(str(item["total_net"])) for item in processed_positions)

    return {
        "decision": "bid",
        "positions": processed_positions,
        "total_net": round_cents(overall_total)
    }