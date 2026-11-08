"""Flet 界面直接调用的本地业务用例层。"""

from __future__ import annotations

from backend.config import DeepSeekConfig, load_deepseek_config
from backend.db import Database, db as default_db
from backend.schemas import (
    BillCategoryUpdate,
    BillCreate,
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

    def status(self) -> dict:
        return {
            "status": "ok",
            "ai_configured": self.deepseek.configured,
            "ai_model": self.deepseek.config.model,
            "database_path": str(self.db.path),
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
        self.balance.set_income_counts_toward_balance(
            payload.income_counts_toward_balance
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
    ) -> list[dict]:
        return self.ledger.list_bills(month, category_id, unclassified)

    async def create_bill(self, payload: BillCreate) -> dict:
        month = payload.occurred_at.strftime("%Y-%m")
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
            )
            category_ids, warning = await self.classifier.classify(month, [candidate])
            category_id = category_ids[0] if category_ids else None

        bill = parsed_from_manual(
            payload.occurred_at,
            payload.amount,
            payload.direction,
            payload.description,
        )
        inserted, result = self.ledger.insert_bill(bill, category_id)
        if not inserted or result is None:
            raise ValueError("账单写入失败")
        return {"bill": result, "warning": warning}

    def update_bill_category(
        self,
        bill_id: int,
        payload: BillCategoryUpdate,
    ) -> dict:
        return self.ledger.update_bill_category(bill_id, payload.category_id)

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

