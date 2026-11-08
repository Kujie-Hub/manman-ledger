"""导入管道：解析结果去重、按月份自动分类并写库。"""

from __future__ import annotations

from collections import defaultdict

from backend.db import Database
from backend.services.classifier import BillClassifier
from backend.services.ledger import LedgerService
from backend.services.statements import ParseResult, ParsedBill


class ImportService:
    def __init__(
        self,
        database: Database,
        ledger: LedgerService | None = None,
        classifier: BillClassifier | None = None,
    ) -> None:
        self.db = database
        self.ledger = ledger or LedgerService(database)
        self.classifier = classifier or BillClassifier(database)

    async def import_bills(self, parsed: ParseResult) -> dict:
        keys = [bill.dedup_key for bill in parsed.bills]
        existing = self.ledger.existing_dedup_keys(keys)
        unique_bills: list[ParsedBill] = []
        seen = set(existing)
        duplicates = 0
        for bill in parsed.bills:
            if bill.dedup_key in seen:
                duplicates += 1
                continue
            seen.add(bill.dedup_key)
            unique_bills.append(bill)

        by_month: dict[str, list[ParsedBill]] = defaultdict(list)
        for bill in unique_bills:
            by_month[bill.month].append(bill)

        categories_by_dedup: dict[str, int | None] = {}
        warnings: list[str] = []
        for month, bills in by_month.items():
            category_ids, warning = await self.classifier.classify(month, bills)
            categories_by_dedup.update(
                {bill.dedup_key: category_id for bill, category_id in zip(bills, category_ids)}
            )
            if warning:
                warnings.append(warning)

        imported_bills: list[dict] = []
        for bill in unique_bills:
            inserted, result = self.ledger.insert_bill(
                bill,
                categories_by_dedup.get(bill.dedup_key),
            )
            if inserted and result:
                imported_bills.append(result)
            else:
                duplicates += 1

        warning = "；".join(dict.fromkeys(warnings)) if warnings else None
        return {
            "source": parsed.source,
            "total_rows": parsed.total_rows,
            "imported": len(imported_bills),
            "duplicates": duplicates,
            "ignored": parsed.ignored,
            "failed": parsed.failed,
            "warning": warning,
            "bills": imported_bills,
        }

