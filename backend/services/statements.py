"""支付宝 CSV 与微信 XLSX 账单解析。

列映射来自 backend/config/statement_columns.json，解析器不写死真实表头。
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path

from openpyxl import load_workbook

from backend.config import RESOURCE_ROOT
from backend.money import normalize_positive_money, to_cents


STATEMENT_CONFIG_PATH = RESOURCE_ROOT / "backend" / "config" / "statement_columns.json"


@dataclass
class ParsedBill:
    occurred_at: datetime
    amount_cents: int
    direction: str
    description: str
    source_category: str | None
    source: str
    external_id: str | None
    dedup_key: str
    raw: dict
    bill_type: str | None = None
    payment_method: str = "其他"
    is_special: bool = False
    deduct_from_balance: bool = True
    affect_balance: bool | None = None
    count_in_balance: bool | None = None

    @property
    def month(self) -> str:
        return self.occurred_at.strftime("%Y-%m")


@dataclass
class ParseResult:
    source: str
    bills: list[ParsedBill]
    total_rows: int
    ignored: int
    failed: int


def _normalize_header(value: object) -> str:
    return str(value or "").strip().replace("\ufeff", "").replace("\t", "")


def _normalize_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().replace("\t", "")


def _load_config() -> dict:
    with STATEMENT_CONFIG_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def _detect_source(headers: list[str], config: dict) -> str:
    header_set = set(headers)
    for source, source_config in config.items():
        if set(source_config["signature_columns"]).issubset(header_set):
            return source
    raise ValueError(
        "无法识别账单格式。请选择个人对账导出的支付宝 CSV 或微信 XLSX；"
        "当前表头："
        + "、".join(header for header in headers if header)
    )


def _find_header(rows: list[list[object]], signature_sets: list[list[str]]) -> int:
    required_sets = [set(signatures) for signatures in signature_sets]
    for index, row in enumerate(rows[:50]):
        headers = {_normalize_header(value) for value in row}
        if any(required.issubset(headers) for required in required_sets):
            return index
    raise ValueError("未找到账单表头，请确认文件是个人对账版本")


def _read_csv_rows(data: bytes) -> list[list[str]]:
    config = _load_config()
    encoding_candidates = []
    for item in config.values():
        encoding_candidates.extend(item.get("encoding_candidates", []))
    encoding_candidates.extend(["utf-8-sig", "gb18030"])

    text = None
    last_error: Exception | None = None
    for encoding in dict.fromkeys(encoding_candidates):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    if text is None:
        raise ValueError("无法识别 CSV 文件编码") from last_error

    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    return list(csv.reader(StringIO(text), delimiter=delimiter))


def _read_xlsx_rows(data: bytes) -> list[list[object]]:
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("无法读取 XLSX 文件，请确认文件未损坏") from exc
    try:
        sheet = workbook.active
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = _normalize_cell(value)
    if not text:
        raise ValueError("日期为空")
    try:
        numeric = float(text)
        if 20000 < numeric < 100000:
            return datetime(1899, 12, 30) + timedelta(days=numeric)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError(f"无法识别日期：{text}") from exc


def _normalize_direction(value: object) -> str | None:
    text = _normalize_cell(value)
    if text in {"支出", "付款", "借"}:
        return "expense"
    if text in {"收入", "收款", "贷"}:
        return "income"
    return None


def _compose_description(values: dict[str, str]) -> str:
    description = values.get("description", "")
    counterparty = values.get("counterparty", "")
    transaction_type = values.get("transaction_type", "")
    parts: list[str] = []
    if description and description != "/":
        parts.append(description)
    elif counterparty and counterparty != "/":
        parts.append(counterparty)
    if counterparty and counterparty != "/" and counterparty not in (parts[0] if parts else ""):
        parts.append(counterparty)
    if not parts and transaction_type and transaction_type != "/":
        parts.append(transaction_type)
    return " · ".join(dict.fromkeys(parts)) or "未提供描述"


def _payment_method(source: str, raw_value: str) -> str:
    text = raw_value.strip()
    if "信用卡" in text:
        return "信用卡"
    if "银行卡" in text or "银行" in text or "储蓄卡" in text:
        return "银行卡"
    if "现金" in text:
        return "现金"
    if "微信" in text or source == "wechat":
        return "微信"
    if "支付宝" in text or source == "alipay":
        return "支付宝"
    return "其他"


def _dedup_key(
    source: str,
    external_id: str | None,
    occurred_at: datetime,
    amount_cents: int,
    direction: str,
    description: str,
) -> str:
    basis = (
        f"{source}:{external_id}"
        if external_id
        else f"{source}:{occurred_at.isoformat()}:{amount_cents}:{direction}:{description}"
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def parse_statement(data: bytes, filename: str) -> ParseResult:
    extension = Path(filename).suffix.lower()
    stripped = data.lstrip()
    if stripped.startswith(b"PK"):
        rows = _read_xlsx_rows(data)
    elif extension == ".csv" or stripped.startswith(b"---"):
        rows = _read_csv_rows(data)
    elif extension in {".xlsx", ".xlsm"}:
        rows = _read_xlsx_rows(data)
    else:
        raise ValueError("仅支持支付宝 CSV 和微信 XLSX 文件")

    config = _load_config()
    signature_sets = [
        signature_set
        for source_config in config.values()
        if (signature_set := source_config["signature_columns"])
    ]
    header_index = _find_header(rows, signature_sets)
    headers = [_normalize_header(value) for value in rows[header_index]]
    source = _detect_source(headers, config)
    source_config = config[source]
    column_map = source_config["columns"]

    positions = {header: index for index, header in enumerate(headers) if header}
    missing = [column for column in column_map.values() if column not in positions]
    if missing:
        raise ValueError("账单缺少必要列：" + "、".join(missing))

    bills: list[ParsedBill] = []
    ignored = 0
    failed = 0
    for row in rows[header_index + 1 :]:
        values = {
            key: _normalize_cell(row[positions[column]]) if positions[column] < len(row) else ""
            for key, column in column_map.items()
        }
        if not any(values.values()):
            continue
        direction = _normalize_direction(values["direction"])
        if direction is None:
            ignored += 1
            continue
        status = values.get("status", "")
        if any(keyword in status for keyword in source_config.get("ignored_status_keywords", [])):
            ignored += 1
            continue
        try:
            occurred_at = _parse_datetime(row[positions[column_map["date"]]])
            amount = normalize_positive_money(row[positions[column_map["amount"]]])
            amount_cents = to_cents(amount)
            description = _compose_description(values)
            source_category = values.get("transaction_type") or None
            external_id = values.get("external_id") or None
            payment_method = _payment_method(
                source,
                values.get("payment_method", ""),
            )
            raw = {column: values.get(key, "") for key, column in column_map.items()}
            bills.append(
                ParsedBill(
                    occurred_at=occurred_at,
                    amount_cents=amount_cents,
                    direction=direction,
                    description=description,
                    source_category=source_category,
                    source=source,
                    external_id=external_id,
                    dedup_key=_dedup_key(
                        source,
                        external_id,
                        occurred_at,
                        amount_cents,
                        direction,
                        description,
                    ),
                    raw=raw,
                    payment_method=payment_method,
                )
            )
        except (ValueError, ArithmeticError, IndexError):
            failed += 1

    return ParseResult(
        source=source,
        bills=bills,
        total_rows=max(0, len(rows) - header_index - 1),
        ignored=ignored,
        failed=failed,
    )


def parsed_from_manual(
    occurred_at: datetime,
    amount: Decimal,
    direction: str,
    description: str,
    bill_type: str | None = None,
    payment_method: str = "其他",
    is_special: bool = False,
    deduct_from_balance: bool = True,
    affect_balance: bool | None = None,
    count_in_balance: bool | None = None,
) -> ParsedBill:
    desc = description.strip()
    occurred_at = occurred_at.replace(tzinfo=None)
    amount_cents = to_cents(amount)
    unique = hashlib.sha256(
        f"manual:{datetime.now().isoformat()}:{occurred_at.isoformat()}:{amount_cents}".encode()
    ).hexdigest()
    return ParsedBill(
        occurred_at=occurred_at,
        amount_cents=amount_cents,
        direction=direction,
        description=desc,
        source_category=None,
        source="manual",
        external_id=None,
        dedup_key=unique,
        raw={},
        bill_type=bill_type or direction,
        payment_method=payment_method,
        is_special=is_special,
        deduct_from_balance=deduct_from_balance,
        affect_balance=affect_balance,
        count_in_balance=count_in_balance,
    )

