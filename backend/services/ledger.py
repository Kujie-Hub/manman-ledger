"""分类、账单查询和去重写入的共享业务逻辑。"""

from __future__ import annotations

import json
import sqlite3
from backend.db import Database
from backend.money import from_cents, to_cents
from backend.schemas import BillUpdate, CategoryCreate, CategoryUpdate
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
                        SELECT SUM(b.amount_cents) FROM bills b
                        WHERE b.app_category_id = c.id AND b.bill_type = c.kind
                    ), 0) AS actual_cents,
                    COALESCE((
                        SELECT SUM(b.amount_cents) FROM bills b
                        WHERE b.app_category_id = c.id AND b.bill_type = 'transfer'
                    ), 0) AS transfer_cents,
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
            "transfer_actual": from_cents(int(row["transfer_cents"])),
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
                    "SELECT id, name, kind FROM categories WHERE id = ? AND month_id = ?",
                    (category_id, month_id),
                ).fetchone()
                if category is None or category["kind"] != bill.direction:
                    category_id = None
            category_name = category["name"] if category is not None else None
            if category_name in {"转账-转入", "转账-转出"}:
                bill_type = "transfer"
            else:
                bill_type = bill.bill_type or bill.direction
            affect_balance = (
                bill.affect_balance
                if bill.affect_balance is not None
                else bill_type != "income"
            )
            count_in_balance = (
                bill.count_in_balance
                if bill.count_in_balance is not None
                else True
            )
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO bills(
                        month_id, occurred_at, amount_cents, direction, bill_type,
                        description, source_category, app_category_id, source, external_id,
                        dedup_key, raw_json, is_special, deduct_from_balance,
                        affect_balance, payment_method, count_in_balance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        month_id,
                        bill.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
                        bill.amount_cents,
                        bill.direction,
                        bill_type,
                        bill.description,
                        bill.source_category,
                        category_id,
                        bill.source,
                        bill.external_id,
                        bill.dedup_key,
                        json.dumps(bill.raw, ensure_ascii=False),
                        1 if bill.is_special else 0,
                        1 if bill.deduct_from_balance else 0,
                        1 if affect_balance else 0,
                        bill.payment_method or "其他",
                        1 if count_in_balance else 0,
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
        payment_method: str | None = None,
    ) -> list[dict]:
        clauses = ["m.month = ?"]
        params: list = [month]
        if unclassified:
            clauses.append("b.app_category_id IS NULL")
        elif category_id is not None:
            clauses.append("b.app_category_id = ?")
            params.append(category_id)
        if payment_method:
            clauses.append("b.payment_method = ?")
            params.append(payment_method)
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
            "type": row["bill_type"],
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
            "is_special": bool(row["is_special"]),
            "isSpecial": bool(row["is_special"]),
            "deduct_from_balance": bool(row["deduct_from_balance"]),
            "deductFromBalance": bool(row["deduct_from_balance"]),
            "affect_balance": bool(row["affect_balance"]),
            "affectBalance": bool(row["affect_balance"]),
            "payment_method": row["payment_method"] or "其他",
            "paymentMethod": row["payment_method"] or "其他",
            "is_refunded": bool(row["is_refunded"]),
            "isRefunded": bool(row["is_refunded"]),
            "refund_of_bill_id": (
                str(row["refund_of_bill_id"])
                if row["refund_of_bill_id"] is not None
                else None
            ),
            "refundOfBillId": (
                str(row["refund_of_bill_id"])
                if row["refund_of_bill_id"] is not None
                else None
            ),
            "count_in_balance": bool(row["count_in_balance"]),
            "countInBalance": bool(row["count_in_balance"]),
            "created_at": row["created_at"],
        }

    def update_bill(self, bill_id: int, payload: BillUpdate) -> dict:
        with self.db.transaction() as connection:
            bill = connection.execute(
                "SELECT * FROM bills WHERE id = ?",
                (bill_id,),
            ).fetchone()
            if bill is None:
                raise ValueError("账单不存在")
            bill_type = payload.type.value if payload.type is not None else bill["bill_type"]
            direction = payload.direction.value if payload.direction is not None else bill["direction"]
            if bill_type != "transfer":
                direction = bill_type
            affect_value = payload.affect_balance
            count_value = payload.count_in_balance
            if bill_type != bill["bill_type"]:
                if affect_value is None:
                    affect_value = bill_type != "income"
                if count_value is None:
                    count_value = True
            if payload.category_id is not None:
                category_id = payload.category_id
            elif "category_id" in payload.model_fields_set:
                category_id = None
            elif bill_type != bill["bill_type"] or direction != bill["direction"]:
                category_id = None
            else:
                category_id = bill["app_category_id"]
            if category_id is not None:
                category = connection.execute(
                    "SELECT kind, month_id FROM categories WHERE id = ?",
                    (category_id,),
                ).fetchone()
                if category is None or int(category["month_id"]) != int(bill["month_id"]):
                    raise ValueError("分类不存在或不属于账单月份")
                if category["kind"] != direction:
                    raise ValueError("支出账单只能归入支出分类，收入账单只能归入收入分类")
            def bind_bool(value):
                return None if value is None else (1 if value else 0)

            connection.execute(
                """
                UPDATE bills
                SET bill_type = ?,
                    direction = ?,
                    app_category_id = ?,
                    is_special = COALESCE(?, is_special),
                    deduct_from_balance = COALESCE(?, deduct_from_balance),
                    affect_balance = COALESCE(?, affect_balance),
                    payment_method = COALESCE(?, payment_method),
                    is_refunded = COALESCE(?, is_refunded),
                    count_in_balance = COALESCE(?, count_in_balance),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    bill_type,
                    direction,
                    category_id,
                    bind_bool(payload.is_special),
                    bind_bool(payload.deduct_from_balance),
                    bind_bool(affect_value),
                    payload.payment_method,
                    bind_bool(payload.is_refunded),
                    bind_bool(count_value),
                    bill_id,
                ),
            )
        result = self.get_bill(bill_id)
        if result is None:
            raise ValueError("账单不存在")
        return result

    def mark_transfer_refunded(self, bill_id: int) -> dict:
        with self.db.transaction() as connection:
            original = connection.execute(
                "SELECT * FROM bills WHERE id = ?",
                (bill_id,),
            ).fetchone()
            if original is None:
                raise ValueError("账单不存在")
            if original["bill_type"] != "transfer" or original["direction"] != "expense":
                raise ValueError("只有转账转出账单可以标记退还")
            linked = connection.execute(
                "SELECT * FROM bills WHERE refund_of_bill_id = ? ORDER BY id LIMIT 1",
                (bill_id,),
            ).fetchone()
            if linked is None:
                category = connection.execute(
                    """
                    SELECT id FROM categories
                    WHERE month_id = ? AND kind = 'income' AND name = '转账-转入'
                    LIMIT 1
                    """,
                    (original["month_id"],),
                ).fetchone()
                dedup = f"refund:{bill_id}"
                cursor = connection.execute(
                    """
                    INSERT INTO bills(
                        month_id, occurred_at, amount_cents, direction, bill_type,
                        description, source_category, app_category_id, source, external_id,
                        dedup_key, raw_json, is_special, deduct_from_balance,
                        affect_balance, payment_method, count_in_balance,
                        is_refunded, refund_of_bill_id
                    ) VALUES (?, ?, ?, 'income', 'transfer',
                        ?, '退款', ?, ?, ?, ?, '{}', 0, 0,
                        ?, ?, ?, 0, ?)
                    """,
                    (
                        original["month_id"],
                        original["occurred_at"],
                        original["amount_cents"],
                        f"退还 · {original['description']}",
                        int(category["id"]) if category else None,
                        original["source"],
                        f"refund-of-{bill_id}",
                        dedup,
                        original["affect_balance"],
                        original["payment_method"],
                        original["count_in_balance"],
                        bill_id,
                    ),
                )
                refund_id = int(cursor.lastrowid)
            else:
                refund_id = int(linked["id"])
            connection.execute(
                """
                UPDATE bills
                SET is_refunded = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (bill_id,),
            )
        return {
            "original": self.get_bill(bill_id),
            "refund": self.get_bill(refund_id),
        }

