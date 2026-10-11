"""pricing.py against hand-computed examples. All numbers are invented, none come from the challenge websites."""
import unittest
from decimal import Decimal as D

import pricing as p


def line(position="1", quantity=10, material="M-1", price="100.00", **kw):
    kw.setdefault("condition", p.PriceCondition(scales=[p.PriceScale(1, D(price))]))
    return p.Line(position=position, quantity=quantity, material=material, **kw)


class ScalePrice(unittest.TestCase):
    def test_highest_reached_scale(self):
        c = p.PriceCondition(scales=[p.PriceScale(1, D("10")), p.PriceScale(50, D("9")), p.PriceScale(100, D("8"))])
        self.assertEqual(p.scale_price(c, 49), D("10.00"))
        self.assertEqual(p.scale_price(c, 50), D("9.00"))
        self.assertEqual(p.scale_price(c, 500), D("8.00"))

    def test_price_unit_and_fx_rounded_half_up(self):
        c = p.PriceCondition(scales=[p.PriceScale(1, D("1234.5"))], currency="CHF", price_unit=100,
                             eur_per_unit=D("1.05"))
        self.assertEqual(p.scale_price(c, 1), D("12.96"))  # 12.345 * 1.05 = 12.96225


class Discount(unittest.TestCase):
    def test_threshold_inclusive_and_failing(self):
        lines = [line(quantity=10, price="100")]  # undiscounted 1000.00
        ok = p.Order("T", lines, approval=p.Approval(D("5"), min_order_value=D("1000.00")))
        fail = p.Order("T", lines, approval=p.Approval(D("5"), min_order_value=D("1000.01")))
        self.assertEqual(p.price_order(ok)["discount_percent"], 5.0)
        self.assertEqual(p.price_order(fail)["discount_percent"], 0.0)

    def test_scopes(self):
        kw = dict(quantity=10, price="100", mass_surcharge_per_unit=D("10"))
        order = p.Order("T", [line("1", discount_scope=p.SCOPE_BASE, **kw),
                              line("2", discount_scope=p.SCOPE_BASE_AND_SURCHARGE, **kw),
                              line("3", discount_scope=p.SCOPE_NONE, **kw)],
                        approval=p.Approval(D("10")))
        prices = [pos["unit_price"] for pos in p.price_order(order)["positions"]]
        self.assertEqual(prices, [100.0, 99.0, 110.0])  # 90+10, 90+9, 100+10


class LeadTime(unittest.TestCase):
    def test_shared_stock_covers_all_or_none(self):
        common = dict(material="M", stock=30, stock_lead_time_weeks=1, production_lead_time_weeks=6)
        covered = p.price_order(p.Order("T", [line("1", 10, **common), line("2", 20, **common)]))
        short = p.price_order(p.Order("T", [line("1", 10, **common), line("2", 21, **common)]))
        self.assertEqual([x["lead_time_weeks"] for x in covered["positions"]], [1, 1])
        self.assertEqual([x["lead_time_weeks"] for x in short["positions"]], [6, 6])

    def test_commitment_needs_matching_material_and_ceiling(self):
        common = dict(stock=0, stock_lead_time_weeks=1, production_lead_time_weeks=6)
        within = line("1", 10, material="M", commitment=p.Commitment("M", 10, 3), **common)
        over = line("1", 11, material="M", commitment=p.Commitment("M", 10, 3), **common)
        other = line("1", 10, material="M", commitment=p.Commitment("X", 10, 3), **common)
        self.assertEqual(p.lead_time(within, {"M": 10}), 3)
        self.assertEqual(p.lead_time(over, {"M": 11}), 6)
        self.assertEqual(p.lead_time(other, {"M": 10}), 6)


class Allocation(unittest.TestCase):
    def test_largest_remainder_ties_by_position(self):
        self.assertEqual(p.allocate_cents(D("0.10"), {"2": D(1), "1": D(1), "3": D(1)}),
                         {"1": D("0.04"), "2": D("0.03"), "3": D("0.03")})

    def test_position_order_is_numeric(self):
        self.assertLess(p._position_key("01.02"), p._position_key("01.10"))

    def test_marginal_bonus_bands(self):
        bands = [p.BonusBand(D(0), D(1000), D(1)), p.BonusBand(D(1000), None, D(2))]
        self.assertEqual(p.project_bonus(bands, D("1500")), D("20.00"))  # 10 + 10
        self.assertEqual(p.project_bonus(bands, D("800")), D("8.00"))


class FinalPrice(unittest.TestCase):
    def test_components_rounded_once_at_the_end(self):
        # base 100, 10 % off -> 90; setup 10 + carton 2 x 5 = 20 over 3 pieces -> 6.6667; total 96.67 per piece
        order = p.Order("T", [line("1", 3, setup_fee=D("10"), carton_size=2, carton_price=D("5"))],
                        approval=p.Approval(D("10")))
        answer = p.price_order(order)
        pos = answer["positions"][0]
        self.assertEqual(pos["unit_price"], 96.67)
        self.assertEqual(pos["packaging_per_unit"], 3.33)
        self.assertEqual(pos["surcharge_per_unit"], 3.33)
        self.assertEqual(answer["total_net"], 290.01)
        self.assertEqual(pos["quantity"], 3)  # cartons never change the bid quantity

    def test_no_bid_when_a_material_is_missing(self):
        answer = p.price_order(p.Order("T", [line("1"), line("2", material=None)], approval=p.Approval(D("5"))))
        self.assertEqual((answer["decision"], answer["positions"], answer["total_net"], answer["discount_percent"]),
                         ("no_bid", [], 0, 0))


if __name__ == "__main__":
    unittest.main()
