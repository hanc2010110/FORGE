from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from pydantic import ValidationError

from forge_core.constraints import (
    BOMLine,
    CostEvaluation,
    InterfaceContract,
    InterfaceSignal,
    QuoteSnapshot,
    evaluate_cost,
)
from forge_core.models import Quantity, SourceRef, Verdict


def quantity(value: float, unit: str, dimension: str) -> Quantity:
    return Quantity(
        value=value,
        unit=unit,
        dimension=dimension,
        source=SourceRef(kind="user", identifier="test:quantity"),
    )


def signal(**updates: object) -> InterfaceSignal:
    values: dict[str, object] = {
        "name": "fan_pwm",
        "pin": "J3-4",
        "direction": "output",
        "voltage_min": quantity(0.0, "V", "voltage"),
        "voltage_max": quantity(3.3, "V", "voltage"),
        "command_min": quantity(0.0, "%", "ratio"),
        "command_max": quantity(100.0, "%", "ratio"),
        "safe_value": quantity(0.0, "%", "ratio"),
    }
    values.update(updates)
    return InterfaceSignal.model_validate(values)


def quote(**updates: object) -> QuoteSnapshot:
    observed = datetime(2026, 8, 27, tzinfo=UTC)
    values: dict[str, object] = {
        "quote_id": "quote-1",
        "part_number": "FAN-120",
        "supplier": "supplier",
        "region": "KR",
        "currency": "USD",
        "unit_price": Decimal("10.00"),
        "minimum_quantity": 1,
        "observed_at": observed,
        "expires_at": observed + timedelta(days=30),
        "shipping_included": False,
        "shipping_cost": Decimal("5.00"),
        "tax_included": False,
        "tax_cost": Decimal("2.00"),
        "source_url": "https://supplier.example/FAN-120",
        "source_hash": "sha256:" + "a" * 64,
    }
    values.update(updates)
    return QuoteSnapshot.model_validate(values)


class InterfaceContractTests(unittest.TestCase):
    def test_valid_interface_contract(self) -> None:
        contract = InterfaceContract(
            contract_id="fan-interface-1",
            protocol_schema_hash="sha256:" + "b" * 64,
            signals=(signal(),),
        )

        safe_value = contract.signals[0].safe_value
        assert safe_value is not None
        self.assertEqual(safe_value.value, 0.0)

    def test_duplicate_pin_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            InterfaceContract(
                contract_id="bad-interface",
                protocol_schema_hash="sha256:" + "b" * 64,
                signals=(signal(), signal(name="fan_tach")),
            )

    def test_voltage_and_safe_ranges_are_enforced(self) -> None:
        with self.assertRaises(ValidationError):
            signal(voltage_min=quantity(0.0, "A", "current"))

        with self.assertRaises(ValidationError):
            signal(voltage_max=quantity(3.3, "A", "current"))

        with self.assertRaises(ValidationError):
            signal(safe_value=quantity(120.0, "%", "ratio"))

    def test_command_range_and_safe_state_are_complete(self) -> None:
        with self.assertRaises(ValidationError):
            signal(command_max=None)

        with self.assertRaises(ValidationError):
            signal(command_max=quantity(100.0, "rpm", "angular_speed"))

        with self.assertRaises(ValidationError):
            signal(safe_value=None)

        with self.assertRaises(ValidationError):
            signal(command_min=None, command_max=None)

    def test_duplicate_signal_name_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            InterfaceContract(
                contract_id="bad-interface",
                protocol_schema_hash="sha256:" + "b" * 64,
                signals=(signal(), signal(pin="J3-5")),
            )


class CostEvaluationTests(unittest.TestCase):
    def test_cost_pass_includes_shipping_tax_and_reserve(self) -> None:
        result = evaluate_cost(
            (BOMLine(part_number="FAN-120", quantity=2),),
            (quote(),),
            currency="USD",
            budget_limit=Decimal("30.00"),
            reserve_rate=Decimal("0.10"),
            evaluated_at=datetime(2026, 8, 28, tzinfo=UTC),
        )

        self.assertEqual(result.known_subtotal, Decimal("27.00"))
        self.assertEqual(result.projected_total, Decimal("29.7000"))
        self.assertEqual(result.verdict, Verdict.PASS)

    def test_cost_can_fail_budget(self) -> None:
        result = evaluate_cost(
            (BOMLine(part_number="FAN-120", quantity=2),),
            (quote(),),
            currency="USD",
            budget_limit=Decimal("20.00"),
            reserve_rate=Decimal("0.10"),
            evaluated_at=datetime(2026, 8, 28, tzinfo=UTC),
        )

        self.assertEqual(result.verdict, Verdict.FAIL)

    def test_missing_expired_or_unknown_scope_is_indeterminate(self) -> None:
        line = BOMLine(part_number="FAN-120", quantity=1)
        evaluated = datetime(2026, 8, 28, tzinfo=UTC)
        cases = (
            (),
            (quote(expires_at=datetime(2026, 8, 27, 12, tzinfo=UTC)),),
            (quote(shipping_included=None),),
        )
        for quotes in cases:
            with self.subTest(quotes=quotes):
                result = evaluate_cost(
                    (line,),
                    quotes,
                    currency="USD",
                    budget_limit=Decimal("100"),
                    reserve_rate=Decimal("0.10"),
                    evaluated_at=evaluated,
                )
                self.assertEqual(result.verdict, Verdict.INDETERMINATE)
                self.assertIsNone(result.projected_total)

    def test_quote_requires_timestamp_and_known_excluded_costs(self) -> None:
        with self.assertRaises(ValidationError):
            quote(observed_at=datetime(2026, 8, 27))

        with self.assertRaises(ValidationError):
            quote(shipping_included=False, shipping_cost=None)

        with self.assertRaises(ValidationError):
            quote(tax_included=False, tax_cost=None)

        with self.assertRaises(ValidationError):
            quote(expires_at=datetime(2026, 8, 26, tzinfo=UTC))

    def test_cost_evaluation_contract_rejects_inconsistent_shape(self) -> None:
        common: dict[str, object] = {
            "currency": "USD",
            "known_subtotal": Decimal("0"),
            "budget_limit": Decimal("100"),
            "reserve_rate": Decimal("0.1"),
            "quote_coverage": Decimal("0"),
            "reasons": (),
        }
        with self.assertRaises(ValidationError):
            CostEvaluation.model_validate(
                {
                    **common,
                    "verdict": Verdict.INDETERMINATE,
                    "projected_total": Decimal("1"),
                }
            )

        with self.assertRaises(ValidationError):
            CostEvaluation.model_validate(
                {**common, "verdict": Verdict.PASS, "projected_total": None}
            )

    def test_cost_input_integrity_is_enforced(self) -> None:
        line = BOMLine(part_number="FAN-120", quantity=1)
        base = {
            "bom": (line,),
            "quotes": (quote(),),
            "currency": "USD",
            "budget_limit": Decimal("100"),
            "reserve_rate": Decimal("0.1"),
            "evaluated_at": datetime(2026, 8, 28, tzinfo=UTC),
        }
        invalid_updates: tuple[dict[str, object], ...] = (
            {"evaluated_at": datetime(2026, 8, 28)},
            {"budget_limit": Decimal("NaN")},
            {"reserve_rate": Decimal("-0.1")},
            {"bom": (line, line)},
            {"quotes": (quote(), quote(quote_id="quote-2"))},
        )
        for update in invalid_updates:
            with self.subTest(update=update), self.assertRaises(ValueError):
                cast(Any, evaluate_cost)(**{**base, **update})

    def test_currency_moq_and_empty_required_bom_are_indeterminate(self) -> None:
        evaluated = datetime(2026, 8, 28, tzinfo=UTC)
        cases = (
            (
                (BOMLine(part_number="FAN-120", quantity=1),),
                (quote(currency="KRW"),),
                "currency_mismatch:FAN-120",
            ),
            (
                (BOMLine(part_number="FAN-120", quantity=1),),
                (quote(minimum_quantity=10),),
                "minimum_quantity_not_met:FAN-120",
            ),
            (
                (BOMLine(part_number="FAN-120", quantity=1, required=False),),
                (quote(),),
                "no_required_bom_lines",
            ),
        )
        for bom, quotes, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                result = evaluate_cost(
                    bom,
                    quotes,
                    currency="USD",
                    budget_limit=Decimal("100"),
                    reserve_rate=Decimal("0.1"),
                    evaluated_at=evaluated,
                )
                self.assertEqual(result.verdict, Verdict.INDETERMINATE)
                self.assertIn(expected_reason, result.reasons)

    def test_included_shipping_and_tax_add_no_extra_cost(self) -> None:
        result = evaluate_cost(
            (BOMLine(part_number="FAN-120", quantity=1),),
            (
                quote(
                    shipping_included=True,
                    shipping_cost=None,
                    tax_included=True,
                    tax_cost=None,
                ),
            ),
            currency="USD",
            budget_limit=Decimal("20"),
            reserve_rate=Decimal("0"),
            evaluated_at=datetime(2026, 8, 28, tzinfo=UTC),
        )

        self.assertEqual(result.projected_total, Decimal("10.00"))


if __name__ == "__main__":
    unittest.main()
