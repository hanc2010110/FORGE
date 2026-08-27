from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from forge_core.models import ContractModel, Quantity, Verdict


class InterfaceSignal(ContractModel):
    name: str = Field(min_length=1)
    pin: str = Field(min_length=1)
    direction: Literal["input", "output", "bidirectional"]
    voltage_min: Quantity
    voltage_max: Quantity
    command_min: Quantity | None = None
    command_max: Quantity | None = None
    safe_value: Quantity | None = None

    @model_validator(mode="after")
    def validate_signal_ranges(self) -> InterfaceSignal:
        if self.voltage_min.dimension != "voltage":
            raise ValueError("signal voltage range must use voltage dimension")
        if (
            self.voltage_min.unit != self.voltage_max.unit
            or self.voltage_min.dimension != self.voltage_max.dimension
            or self.voltage_min.value > self.voltage_max.value
        ):
            raise ValueError("invalid signal voltage range")

        if (self.command_min is None) != (self.command_max is None):
            raise ValueError("command range requires both minimum and maximum")
        if (
            self.command_min is not None
            and self.command_max is not None
            and (
                self.command_min.unit != self.command_max.unit
                or self.command_min.dimension != self.command_max.dimension
                or self.command_min.value > self.command_max.value
            )
        ):
            raise ValueError("invalid command range")
        if self.direction != "input" and self.safe_value is None:
            raise ValueError("controllable signals require safe_value")
        if self.safe_value is not None:
            if self.command_min is None or self.command_max is None:
                raise ValueError("safe_value requires a command range")
            if (
                self.safe_value.unit != self.command_min.unit
                or self.safe_value.dimension != self.command_min.dimension
                or not self.command_min.value
                <= self.safe_value.value
                <= self.command_max.value
            ):
                raise ValueError("safe_value must be inside the command range")
        return self


class InterfaceContract(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    contract_id: str = Field(min_length=1)
    protocol_schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signals: tuple[InterfaceSignal, ...]

    @model_validator(mode="after")
    def validate_unique_signals_and_pins(self) -> InterfaceContract:
        names = [signal.name for signal in self.signals]
        pins = [signal.pin for signal in self.signals]
        if len(names) != len(set(names)):
            raise ValueError("interface signal names must be unique")
        if len(pins) != len(set(pins)):
            raise ValueError("interface pins must be unique")
        return self


class BOMLine(ContractModel):
    part_number: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    required: bool = True


class QuoteSnapshot(ContractModel):
    quote_id: str = Field(min_length=1)
    part_number: str = Field(min_length=1)
    supplier: str = Field(min_length=1)
    region: str = Field(min_length=1)
    currency: str = Field(min_length=3, max_length=3)
    unit_price: Decimal = Field(ge=0)
    minimum_quantity: int = Field(ge=1)
    observed_at: datetime
    expires_at: datetime
    shipping_included: bool | None
    shipping_cost: Decimal | None = Field(default=None, ge=0)
    tax_included: bool | None
    tax_cost: Decimal | None = Field(default=None, ge=0)
    source_url: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("unit_price", "shipping_cost", "tax_cost")
    @classmethod
    def money_must_be_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("money values must be finite")
        return value

    @model_validator(mode="after")
    def validate_quote_scope(self) -> QuoteSnapshot:
        if self.observed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("quote timestamps must include timezone")
        if self.expires_at <= self.observed_at:
            raise ValueError("quote expiration must be after observation")
        if self.shipping_included is False and self.shipping_cost is None:
            raise ValueError("excluded shipping requires shipping_cost")
        if self.tax_included is False and self.tax_cost is None:
            raise ValueError("excluded tax requires tax_cost")
        return self


class CostEvaluation(ContractModel):
    verdict: Verdict
    currency: str = Field(min_length=3, max_length=3)
    known_subtotal: Decimal = Field(ge=0)
    projected_total: Decimal | None = Field(default=None, ge=0)
    budget_limit: Decimal = Field(ge=0)
    reserve_rate: Decimal = Field(ge=0)
    quote_coverage: Decimal = Field(ge=0, le=1)
    reasons: tuple[str, ...]

    @model_validator(mode="after")
    def validate_cost_verdict_shape(self) -> CostEvaluation:
        if self.verdict is Verdict.INDETERMINATE and self.projected_total is not None:
            raise ValueError("indeterminate cost cannot have projected_total")
        if self.verdict is not Verdict.INDETERMINATE and self.projected_total is None:
            raise ValueError("determinate cost requires projected_total")
        return self


def evaluate_cost(
    bom: Iterable[BOMLine],
    quotes: Iterable[QuoteSnapshot],
    *,
    currency: str,
    budget_limit: Decimal,
    reserve_rate: Decimal,
    evaluated_at: datetime,
) -> CostEvaluation:
    lines = tuple(bom)
    quote_list = tuple(quotes)
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluation time must include timezone")
    if not budget_limit.is_finite() or not reserve_rate.is_finite():
        raise ValueError("budget and reserve must be finite")
    if budget_limit < 0 or reserve_rate < 0:
        raise ValueError("budget and reserve cannot be negative")

    part_numbers = [line.part_number for line in lines]
    if len(part_numbers) != len(set(part_numbers)):
        raise ValueError("BOM part numbers must be unique")
    quote_parts = [quote.part_number for quote in quote_list]
    if len(quote_parts) != len(set(quote_parts)):
        raise ValueError("only one quote snapshot is allowed per part")

    quote_by_part = {quote.part_number: quote for quote in quote_list}
    required = tuple(line for line in lines if line.required)
    covered = 0
    subtotal = Decimal(0)
    reasons: list[str] = []

    for line in required:
        quote = quote_by_part.get(line.part_number)
        if quote is None:
            reasons.append(f"missing_quote:{line.part_number}")
            continue
        if quote.currency != currency:
            reasons.append(f"currency_mismatch:{line.part_number}")
            continue
        if quote.expires_at <= evaluated_at:
            reasons.append(f"expired_quote:{line.part_number}")
            continue
        if quote.minimum_quantity > line.quantity:
            reasons.append(f"minimum_quantity_not_met:{line.part_number}")
            continue
        if quote.shipping_included is None or quote.tax_included is None:
            reasons.append(f"unknown_cost_scope:{line.part_number}")
            continue

        line_total = quote.unit_price * line.quantity
        if quote.shipping_included is False:
            assert quote.shipping_cost is not None
            line_total += quote.shipping_cost
        if quote.tax_included is False:
            assert quote.tax_cost is not None
            line_total += quote.tax_cost
        subtotal += line_total
        covered += 1

    coverage = Decimal(covered) / Decimal(len(required)) if required else Decimal(0)
    if reasons or coverage < 1:
        return CostEvaluation(
            verdict=Verdict.INDETERMINATE,
            currency=currency,
            known_subtotal=subtotal,
            projected_total=None,
            budget_limit=budget_limit,
            reserve_rate=reserve_rate,
            quote_coverage=coverage,
            reasons=tuple(reasons or ("no_required_bom_lines",)),
        )

    projected = subtotal * (Decimal(1) + reserve_rate)
    verdict = Verdict.PASS if projected <= budget_limit else Verdict.FAIL
    return CostEvaluation(
        verdict=verdict,
        currency=currency,
        known_subtotal=subtotal,
        projected_total=projected,
        budget_limit=budget_limit,
        reserve_rate=reserve_rate,
        quote_coverage=coverage,
        reasons=(),
    )
