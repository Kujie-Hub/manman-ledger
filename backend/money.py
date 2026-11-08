"""金额字符串与整数分之间的无损转换。"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def to_cents(value: Decimal | int | str | float) -> int:
    decimal_value = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    return int(decimal_value * 100)


def from_cents(value: int | None) -> str:
    return f"{(int(value or 0) / 100):.2f}"


def normalize_positive_money(value: object) -> Decimal:
    text = str(value or "").strip().replace(",", "").replace("¥", "").replace("￥", "")
    text = text.replace("元", "").strip()
    if not text:
        raise ValueError("金额为空")
    amount = Decimal(text).copy_abs().quantize(CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    return amount

