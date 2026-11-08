"""分类、账单查询和去重写入的共享业务逻辑。"""

from __future__ import annotations

import json
import sqlite3
from backend.db import Database
from backend.money import from_cents, to_cents
from backend.schemas import CategoryCreate, CategoryUpdate
from backend.services.balance import BalanceService
from backend.services.statements import ParsedBill


class LedgerService:
    def __init__(self, database: Database) -> None:
        self.db = database
        self.balance = BalanceService(database)

    def list_months(self) -> list[dict]:
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT month, total_balance_cents, updated_at FROM months ORDER BY month DESC"
            ).fetchall()
        return [
            {
                "month": row["month"],
                "total_balance": from_cents(row["total_balance_cents"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def set_month_balance(self, month: str, total_balance) -> dict:
        month = self.balance.validate_month(month)
        cents = to_cents(total_balance)
        with self.db.transaction() as connection:
            self.balance.ensure_month(connection, month)
            connection.execute(
                """
                UPDATE months
                SET total_balance_cents = ?, updated_at = CURRENT_TIMESTAMP
                WHERE month = ?
                """,
                (cents, month),
            )
        return self.balance.summary(month)

    def create_category(self, month: str, payload: CategoryCreate) -> dict:
        month = self.balance.validate_month(month)
        if payload.kind == "expense" and payload.limit is None:
            raise ValueError("支出分类必须填写额度上限")
        if payload.kind == "income" and payload.limit is not None:
            raise ValueError("收入分类不需要额度上限")
        limit_cents = to_cents(payload.limit) if payload.limit is not None else None
        with self.db.transaction() as connection:
            month_id = self.balance.ensure_month(connection, month)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO categories(month_id, name, kind, limit_cents, counts_toward_balance)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (month_id, payload.name, payload.kind, limit_cents),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("同类型分类名称已存在") from exc
            category_id = int(cursor.lastrowid)
        return self.get_category(month, category_id)

    def update_category(self, month: str, category_id: int, payload: CategoryUpdate) -> dict:
        with self.db.transaction() as connection:
            month_id = self.balance.ensure_month(connection, month)
            category = connection.execute(
                "SELECT * FROM categories WHERE id = ? AND month_id = ?",
                (category_id, month_id),
            ).fetchone()
            if category is None:
                raise ValueError("分类不存在")
            name = payload.name or category["name"]
            limit_cents = category["limit_cents"]
            if category["kind"] == "expense" and payload.limit is not None:
                limit_cents = to_cents(payload.limit)
            try:
                connection.execute(
                    """
                    UPDATE categories
                    SET name = ?, limit_cents = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (name, limit_cents, category_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("同类型分类名称已存在") from exc
        return self.get_category(month, category_id)

    def get_category(self, month: str, category_id: int) -> dict:
        category = next(
            (item for item in self.list_categories(month) if item["id"] == category_id),
            None,
        )
        if category is None:
            raise ValueError("分类不存在")
        return category

    def list_categories(self, month: str) -> list[dict]:
        with self.db.read() as connection:
            rows = connection.execute(
                """
                SELECT c.*, m.month,
                    COALESCE((
                        SELECT SUM(b.amount_cents) FROM bills b WHERE b.app_category_id = c.id
                    ), 0) AS actual_cents,
                    (SELECT COUNT(*) FROM bills b WHERE b.app_category_id = c.id) AS bill_count
                FROM categories c
                JOIN months m ON m.id = c.month_id
                WHERE m.month = ?
                ORDER BY CASE c.kind WHEN 'expense' THEN 0 ELSE 1 END, c.name
                """,
                (month,),
            ).fetchall()
        return [self._category_dict(row) for row in rows]

    @staticmethod
    def _category_dict(row: sqlite3.Row) -> dict:
        actual = int(row["actual_cents"])
        limit_cents = row["limit_cents"]
        return {
            "id": row["id"],
            "month": row["month"],
            "name": row["name"],
            "kind": row["kind"],
            "limit": from_cents(limit_cents) if limit_cents is not None else None,
            "actual": from_cents(actual),
            "remaining": from_cents(int(limit_cents) - actual) if row["kind"] == "expense" else None,
            "bill_count": int(row["bill_count"]),
            "counts_toward_balance": bool(row["counts_toward_balance"]),
        }

    def existing_dedup_keys(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        result: set[str] = set()
        with self.db.read() as connection:
            for index in range(0, len(keys), 500):
                chunk = keys[index : index + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT dedup_key FROM bills WHERE dedup_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                result.update(row["dedup_key"] for row in rows)
        return result

    def insert_bill(
        self,
        bill: ParsedBill,
        category_id: int | None,
    ) -> tuple[bool, dict | None]:
        with self.db.transaction() as connection:
            month_id = self.balance.ensure_month(connection, bill.month)
            category = None
            if category_id is not None:
                category = connection.execute(
                    "SELECT id, kind FROM categories WHERE id = ? AND month_id = ?",
                    (category_id, month_id),
                ).fetchone()
                if category is None or category["kind"] != bill.direction:
                    category_id = None
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO bills(
                        month_id, occurred_at, amount_cents, direction, description,
                        source_category, app_category_id, source, external_id,
                        dedup_key, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        month_id,
                        bill.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
                        bill.amount_cents,
                        bill.direction,
                        bill.description,
                        bill.source_category,
                        category_id,
                        bill.source,
                        bill.external_id,
                        bill.dedup_key,
                        json.dumps(bill.raw, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError:
                return False, None
            bill_id = int(cursor.lastrowid)
        return True, self.get_bill(bill_id)

    def get_bill(self, bill_id: int) -> dict | None:
        with self.db.read() as connection:
            row = connection.execute(
                """
                SELECT b.*, c.name AS category_name, c.kind AS category_kind,
                       p.name AS category_parent_name
                FROM bills b
                LEFT JOIN categories c ON c.id = b.app_category_id
                LEFT JOIN categories p ON p.id = c.parent_id
                WHERE b.id = ?
                """,
                (bill_id,),
            ).fetchone()
        return self._bill_dict(row) if row else None

    def list_bills(
        self,
        month: str,
        category_id: int | None = None,
        unclassified: bool = False,
    ) -> list[dict]:
        clauses = ["m.month = ?"]
        params: list = [month]
        if unclassified:
            clauses.append("b.app_category_id IS NULL")
        elif category_id is not None:
            clauses.append("b.app_category_id = ?")
            params.append(category_id)
        with self.db.read() as connection:
            rows = connection.execute(
                f"""
                SELECT b.*, c.name AS category_name, c.kind AS category_kind,
                       p.name AS category_parent_name
                FROM bills b
                JOIN months m ON m.id = b.month_id
                LEFT JOIN categories c ON c.id = b.app_category_id
                LEFT JOIN categories p ON p.id = c.parent_id
                WHERE {' AND '.join(clauses)}
                ORDER BY b.occurred_at DESC, b.id DESC
                """,
                params,
            ).fetchall()
        return [self._bill_dict(row) for row in rows]

    @staticmethod
    def _bill_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "month": row["occurred_at"][:7],
            "occurred_at": row["occurred_at"],
            "amount": from_cents(row["amount_cents"]),
            "direction": row["direction"],
            "description": row["description"],
            "source_category": row["source_category"],
            "category_id": row["app_category_id"],
            "category_name": (
                f"{row['category_parent_name']} / {row['category_name']}"
                if row["category_parent_name"]
                else (row["category_name"] or "未分类")
            ),
            "category_kind": row["category_kind"],
            "source": row["source"],
            "created_at": row["created_at"],
        }

    def update_bill_category(self, bill_id: int, category_id: int | None) -> dict:
        with self.db.transaction() as connection:
            bill = connection.execute(
                "SELECT month_id, direction FROM bills WHERE id = ?",
                (bill_id,),
            ).fetchone()
            if bill is None:
                raise ValueError("账单不存在")
            if category_id is not None:
                category = connection.execute(
                    "SELECT kind, month_id FROM categories WHERE id = ?",
                    (category_id,),
                ).fetchone()
                if category is None or int(category["month_id"]) != int(bill["month_id"]):
                    raise ValueError("分类不存在或不属于账单月份")
                if category["kind"] != bill["direction"]:
                    raise ValueError("支出账单只能归入支出分类，收入账单只能归入收入分类")
            connection.execute(
                """
                UPDATE bills
                SET app_category_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (category_id, bill_id),
            )
        result = self.get_bill(bill_id)
        if result is None:
            raise ValueError("账单不存在")
        return result

