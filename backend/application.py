"""Flet 界面直接调用的本地业务用例层。"""

from __future__ import annotations

from backend.config import DeepSeekConfig, load_deepseek_config, payment_methods
from backend.db import Database, db as default_db
from backend.schemas import (
    BillCreate,
    BillUpdate,
    CategoryCreate,
    CategoryUpdate,
    IncomeSettingUpdate,
    MonthUpdate,
)
from backend.services.balance import BalanceService
from backend.services.classifier import BillClassifier
from backend.services.deepseek import DeepSeekClient
from backend.services.imports import ImportService
from backend.services.ledger import LedgerService
from backend.services.screenshot import extract_screenshot_bills
from backend.services.statements import ParseResult, parse_statement, parsed_from_manual


STATEMENT_LIMIT = 25 * 1024 * 1024
SCREENSHOT_LIMIT = 32 * 1024 * 1024


class LedgerApplication:
    """不依赖 HTTP 的用例编排。"""

    def __init__(
        self,
        database: Database | None = None,
        deepseek_config: DeepSeekConfig | None = None,
    ) -> None:
        self.db = database or default_db
        self.balance = BalanceService(self.db)
        self.ledger = LedgerService(self.db)
        self.deepseek_config = deepseek_config or load_deepseek_config()
        self.deepseek = DeepSeekClient(self.deepseek_config)
        self.classifier = BillClassifier(self.db, self.deepseek)
        self.import_service = ImportService(
            self.db,
            ledger=self.ledger,
            classifier=self.classifier,
        )
        self.db.initialize()

    @staticmethod
    def _payment_method(value: str | None) -> str:
        allowed = payment_methods()
        return value if value in allowed else "其他"

    def status(self) -> dict:
        return {
            "status": "ok",
            "ai_configured": self.deepseek.configured,
            "ai_model": self.deepseek.config.model,
            "database_path": str(self.db.path),
            "payment_methods": payment_methods(),
        }

    def set_deepseek_key(self, api_key: str) -> None:
        self.deepseek_config = DeepSeekConfig(
            api_key=api_key.strip(),
            base_url=self.deepseek_config.base_url,
            model=self.deepseek_config.model,
            timeout_seconds=self.deepseek_config.timeout_seconds,
        )
        self.deepseek = DeepSeekClient(self.deepseek_config)
        self.classifier = BillClassifier(self.db, self.deepseek)
        self.import_service = ImportService(
            self.db,
            ledger=self.ledger,
            classifier=self.classifier,
        )

    def list_months(self) -> list[dict]:
        return self.ledger.list_months()

    def get_month(self, month: str) -> dict:
        with self.db.transaction() as connection:
            self.balance.ensure_month(connection, month)
        return self.balance.summary(month)

    def update_month(self, month: str, payload: MonthUpdate) -> dict:
        if payload.use_carryover:
            return self.ledger.use_month_carryover(month)
        return self.ledger.set_month_balance(month, payload.total_balance)

    def list_categories(self, month: str) -> list[dict]:
        with self.db.transaction() as connection:
            self.balance.ensure_month(connection, month)
        return self.ledger.list_categories(month)

    def create_category(self, month: str, payload: CategoryCreate) -> dict:
        return self.ledger.create_category(month, payload)

    def get_category(self, month: str, category_id: int) -> dict:
        return self.ledger.get_category(month, category_id)

    def update_category(
        self,
        month: str,
        category_id: int,
        payload: CategoryUpdate,
    ) -> dict:
        return self.ledger.update_category(month, category_id, payload)

    def get_settings(self) -> dict:
        return {
            "income_counts_toward_balance": self.balance.get_income_counts_toward_balance(),
        }

    def update_income_setting(self, payload: IncomeSettingUpdate) -> dict:
        self.balance.validate_month(payload.month)
        self.balance.apply_income_switch_to_month(
            payload.month,
            payload.income_counts_toward_balance,
        )
        return {
            "message": "收入计入余额设置已更新",
            "income_counts_toward_balance": payload.income_counts_toward_balance,
            "summary": self.balance.summary(payload.month),
        }

    def list_bills(
        self,
        month: str,
        category_id: int | None = None,
        unclassified: bool = False,
        payment_method: str | None = None,
    ) -> list[dict]:
        return self.ledger.list_bills(
            month,
            category_id,
            unclassified,
            payment_method,
        )

    async def create_bill(self, payload: BillCreate) -> dict:
        month = payload.occurred_at.strftime("%Y-%m")
        payment_method = self._payment_method(payload.payment_method)
        if payload.category_id is not None:
            category = self.ledger.get_category(month, payload.category_id)
            if category["kind"] != payload.direction:
                raise ValueError("支出账单只能归入支出分类，收入账单只能归入收入分类")
            category_id = payload.category_id
            warning = None
        else:
            candidate = parsed_from_manual(
                payload.occurred_at,
                payload.amount,
                payload.direction,
                payload.description,
                payload.type.value,
                payment_method,
                payload.is_special,
                payload.deduct_from_balance,
                payload.affect_balance,
                payload.count_in_balance,
            )
            category_ids, warning = await self.classifier.classify(month, [candidate])
            category_id = category_ids[0] if category_ids else None

        bill = parsed_from_manual(
            payload.occurred_at,
            payload.amount,
            payload.direction,
            payload.description,
            payload.type.value,
            payment_method,
            payload.is_special,
            payload.deduct_from_balance,
            payload.affect_balance,
            payload.count_in_balance,
        )
        inserted, result = self.ledger.insert_bill(bill, category_id)
        if not inserted or result is None:
            raise ValueError("账单写入失败")
        return {"bill": result, "warning": warning}

    def update_bill(
        self,
        bill_id: int,
        payload: BillUpdate,
    ) -> dict:
        if payload.payment_method is not None:
            payload.payment_method = self._payment_method(payload.payment_method)
        return self.ledger.update_bill(bill_id, payload)

    def mark_transfer_refunded(self, bill_id: int) -> dict:
        return self.ledger.mark_transfer_refunded(bill_id)

    def unmark_transfer_refunded(self, bill_id: int) -> dict:
        return self.ledger.unmark_transfer_refunded(bill_id)

    def delete_bill(self, bill_id: int) -> dict:
        return self.ledger.delete_bill(bill_id)

    def clear_bills(self, month: str, unclassified: bool = False) -> dict:
        return self.ledger.clear_bills(month, unclassified)

    async def import_statement(self, data: bytes, filename: str) -> dict:
        if len(data) > STATEMENT_LIMIT:
            raise ValueError("账单文件不能超过 25 MB")
        parsed = parse_statement(data, filename)
        return await self.import_service.import_bills(parsed)

    async def import_screenshot(self, data: bytes) -> dict:
        if len(data) > SCREENSHOT_LIMIT:
            raise ValueError("截图不能超过 32 MB")
        bills, failed = await extract_screenshot_bills(data, self.deepseek)
        parsed = ParseResult(
            source="screenshot",
            bills=bills,
            total_rows=len(bills) + failed,
            ignored=0,
            failed=failed,
        )
        return await self.import_service.import_bills(parsed)


application = LedgerApplication(default_db)

