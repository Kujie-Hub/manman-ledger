"""本地业务调用使用的数据模型与基础校验。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pydantic import BaseModel, Field, field_validator


class CategoryKind(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"


class Direction(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"


class MonthUpdate(BaseModel):
    total_balance: Decimal = Field(ge=0, max_digits=14, decimal_places=2)


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    kind: CategoryKind
    limit: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("分类名称不能为空")
        return cleaned


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    limit: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("分类名称不能为空")
        return cleaned


class BillCreate(BaseModel):
    occurred_at: datetime
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    direction: Direction
    description: str = Field(min_length=1, max_length=300)
    category_id: int | None = Field(default=None, gt=0)

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("商品描述不能为空")
        return cleaned


class BillCategoryUpdate(BaseModel):
    category_id: int | None = Field(default=None, gt=0)


class IncomeSettingUpdate(BaseModel):
    income_counts_toward_balance: bool
    month: str

    @field_validator("month")
    @classmethod
    def validate_month(cls, value: str) -> str:
        datetime.strptime(value, "%Y-%m")
        return value



