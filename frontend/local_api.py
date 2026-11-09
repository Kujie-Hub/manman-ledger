"""Flet 界面调用本地业务用例的适配器。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from backend.application import LedgerApplication
from backend.schemas import (
    BillCreate,
    BillUpdate,
    CategoryCreate,
    CategoryUpdate,
    IncomeSettingUpdate,
    MonthUpdate,
)
from backend.services.deepseek import DeepSeekError


class ApiError(RuntimeError):
    pass


class LocalLedgerGateway:
    """不监听端口，也不发起本地网络请求。"""

    def __init__(self, deepseek_api_key: str = "") -> None:
        self.application = LedgerApplication()
        if deepseek_api_key:
            self.application.set_deepseek_key(deepseek_api_key)

    def set_deepseek_key(self, api_key: str) -> None:
        self.application.set_deepseek_key(api_key)

    def _path_parts(self, path: str) -> list[str]:
        return [part for part in path.split("?")[0].strip("/").split("/") if part]

    @staticmethod
    def _normalize_bill_payload(payload: dict) -> dict:
        aliases = {
            "isSpecial": "is_special",
            "deductFromBalance": "deduct_from_balance",
            "affectBalance": "affect_balance",
            "paymentMethod": "payment_method",
            "isRefunded": "is_refunded",
            "refundOfBillId": "refund_of_bill_id",
            "countInBalance": "count_in_balance",
        }
        normalized = dict(payload)
        for source_key, target_key in aliases.items():
            if source_key in normalized and target_key not in normalized:
                normalized[target_key] = normalized[source_key]
        return normalized

    async def get(self, path: str, params: dict | None = None) -> Any:
        parts = self._path_parts(path)
        try:
            if parts == ["status"]:
                return self.application.status()
            if parts == ["settings"]:
                return self.application.get_settings()
            if parts == ["months"]:
                return self.application.list_months()
            if len(parts) == 2 and parts[0] == "months":
                return self.application.get_month(parts[1])
            if len(parts) == 3 and parts[0] == "months" and parts[2] == "categories":
                return self.application.list_categories(parts[1])
            if len(parts) == 4 and parts[0] == "months" and parts[2] == "categories":
                return self.application.get_category(parts[1], int(parts[3]))
            if parts == ["bills"]:
                query = params or {}
                return self.application.list_bills(
                    str(query["month"]),
                    int(query["category_id"]) if query.get("category_id") else None,
                    str(query.get("unclassified", "false")).lower() in {"1", "true"},
                    str(query["payment_method"]) if query.get("payment_method") else None,
                )
        except (ValueError, DeepSeekError) as exc:
            raise ApiError(str(exc)) from exc
        raise ApiError(f"不支持的本地读取路径：{path}")

    async def post(self, path: str, payload: dict | None = None) -> Any:
        parts = self._path_parts(path)
        payload = payload or {}
        try:
            if len(parts) == 3 and parts[0] == "months" and parts[2] == "categories":
                return self.application.create_category(
                    parts[1],
                    CategoryCreate.model_validate(payload),
                )
            if parts == ["bills"]:
                return await self.application.create_bill(
                    BillCreate.model_validate(self._normalize_bill_payload(payload))
                )
            if len(parts) == 3 and parts[0] == "bills" and parts[2] == "refund":
                return self.application.mark_transfer_refunded(int(parts[1]))
            if (
                len(parts) == 4
                and parts[0] == "bills"
                and parts[2] == "refund"
                and parts[3] == "undo"
            ):
                return self.application.unmark_transfer_refunded(int(parts[1]))
        except (ValueError, DeepSeekError, ValidationError) as exc:
            raise ApiError(str(exc)) from exc
        raise ApiError(f"不支持的本地写入路径：{path}")

    async def put(self, path: str, payload: dict) -> Any:
        parts = self._path_parts(path)
        if len(parts) == 2 and parts[0] == "months":
            try:
                return self.application.update_month(
                    parts[1],
                    MonthUpdate.model_validate(payload),
                )
            except (ValueError, ValidationError) as exc:
                raise ApiError(str(exc)) from exc
        raise ApiError(f"不支持的本地更新路径：{path}")

    async def patch(self, path: str, payload: dict) -> Any:
        parts = self._path_parts(path)
        try:
            if len(parts) == 4 and parts[0] == "months" and parts[2] == "categories":
                return self.application.update_category(
                    parts[1],
                    int(parts[3]),
                    CategoryUpdate.model_validate(payload),
                )
            if len(parts) == 2 and parts[0] == "bills":
                return self.application.update_bill(
                    int(parts[1]),
                    BillUpdate.model_validate(self._normalize_bill_payload(payload)),
                )
            if parts == ["settings", "income-counting"]:
                return self.application.update_income_setting(
                    IncomeSettingUpdate.model_validate(payload)
                )
        except (ValueError, DeepSeekError, ValidationError) as exc:
            raise ApiError(str(exc)) from exc
        raise ApiError(f"不支持的本地更新路径：{path}")

    async def delete(self, path: str, params: dict | None = None) -> Any:
        parts = self._path_parts(path)
        query = params or {}
        try:
            if len(parts) == 2 and parts[0] == "bills":
                return self.application.delete_bill(int(parts[1]))
            if parts == ["bills"]:
                return self.application.clear_bills(
                    str(query["month"]),
                    str(query.get("unclassified", "false")).lower() in {"1", "true"},
                )
        except (ValueError, KeyError, DeepSeekError) as exc:
            raise ApiError(str(exc)) from exc
        raise ApiError(f"不支持的本地删除路径：{path}")

    async def upload(self, path: str, filename: str, data: bytes) -> Any:
        try:
            if path == "/imports/statement":
                return await self.application.import_statement(data, filename)
            if path == "/imports/screenshot":
                return await self.application.import_screenshot(data)
        except (ValueError, DeepSeekError) as exc:
            raise ApiError(str(exc)) from exc
        raise ApiError(f"不支持的本地导入路径：{path}")

