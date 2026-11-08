"""截图格式校验与识别结果转换。"""

from __future__ import annotations

import hashlib
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

from backend.money import normalize_positive_money, to_cents
from backend.services.deepseek import DeepSeekClient, DeepSeekError
from backend.services.prompts import SCREENSHOT_SYSTEM_PROMPT
from backend.services.statements import ParsedBill, _parse_datetime


SUPPORTED_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


def validate_image(data: bytes) -> str:
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("无法识别图片，请选择 JPEG、PNG、GIF 或 WebP") from exc
    media_type = SUPPORTED_FORMATS.get(str(image_format).upper())
    if media_type is None:
        raise ValueError("图片格式不受支持，仅支持 JPEG、PNG、GIF 或 WebP")
    return media_type


def _direction(value: Any) -> str | None:
    text = str(value or "").strip()
    if text in {"支出", "付款", "expense"}:
        return "expense"
    if text in {"收入", "收款", "income"}:
        return "income"
    return None


async def extract_screenshot_bills(
    data: bytes,
    client: DeepSeekClient | None = None,
) -> tuple[list[ParsedBill], int]:
    media_type = validate_image(data)
    response = await (client or DeepSeekClient()).extract_screenshot(
        data,
        media_type,
        SCREENSHOT_SYSTEM_PROMPT,
    )
    transactions = response.get("transactions")
    if not isinstance(transactions, list):
        raise DeepSeekError("截图识别结果缺少 transactions 数组")

    bills: list[ParsedBill] = []
    failed = 0
    image_digest = hashlib.sha256(data).hexdigest()
    for index, transaction in enumerate(transactions):
        try:
            if not isinstance(transaction, dict):
                raise ValueError
            occurred_at = _parse_datetime(transaction.get("date"))
            amount_cents = to_cents(normalize_positive_money(transaction.get("amount")))
            direction = _direction(transaction.get("direction"))
            description = str(transaction.get("description") or "").strip()
            if direction is None or not description:
                raise ValueError
            unique = hashlib.sha256(
                (
                    f"screenshot:{image_digest}:{index}:{occurred_at.isoformat()}:"
                    f"{amount_cents}:{direction}:{description}"
                ).encode("utf-8")
            ).hexdigest()
            bills.append(
                ParsedBill(
                    occurred_at=occurred_at,
                    amount_cents=amount_cents,
                    direction=direction,
                    description=description,
                    source_category=None,
                    source="screenshot",
                    external_id=None,
                    dedup_key=unique,
                    raw=transaction,
                )
            )
        except (ValueError, TypeError, ArithmeticError):
            failed += 1
    return bills, failed

