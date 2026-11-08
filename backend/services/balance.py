"""收入开关和月度可用余额的唯一计算入口。"""

from __future__ import annotations

from backend.db import Database
from backend.money import from_cents


class BalanceService:
    def __init__(self, database: Database) -> None:
        self.db = database

    def get_income_counts_toward_balance(self) -> bool:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                ("income_counts_toward_balance",),
            ).fetchone()
        if row is None:
            return False
        return str(row["value"]).lower() == "true"

    def set_income_counts_toward_balance(self, enabled: bool) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                ("income_counts_toward_balance", "true" if enabled else "false"),
            )

    @staticmethod
    def validate_month(month: str) -> str:
        try:
            year, month_number = month.split("-")
            if len(year) != 4 or not 1 <= int(month_number) <= 12:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValueError("月份格式应为 YYYY-MM") from exc
        return month

    def ensure_month(self, connection, month: str) -> int:
        from backend.services.default_categories import ensure_default_categories_for_month

        month = self.validate_month(month)
        connection.execute(
            "INSERT OR IGNORE INTO months(month, total_balance_cents) VALUES (?, 0)",
            (month,),
        )
        row = connection.execute(
            "SELECT id FROM months WHERE month = ?",
            (month,),
        ).fetchone()
        month_id = int(row["id"])
        ensure_default_categories_for_month(connection, month_id)
        return month_id

    def summary(self, month: str) -> dict:
        month = self.validate_month(month)
        income_enabled = self.get_income_counts_toward_balance()
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT id, total_balance_cents FROM months WHERE month = ?",
                (month,),
            ).fetchone()
            if row is None:
                total_cents = 0
                month_id = None
            else:
                total_cents = int(row["total_balance_cents"])
                month_id = int(row["id"])

            income_cents = 0
            countable_income_cents = 0
            expense_cents = 0
            unclassified_expense_cents = 0
            if month_id is not None:
                income_cents = int(
                    connection.execute(
                        """
                        SELECT COALESCE(SUM(amount_cents), 0) AS total
                        FROM bills
                        WHERE month_id = ? AND direction = 'income'
                        """,
                        (month_id,),
                    ).fetchone()["total"]
                )
                countable_income_cents = int(
                    connection.execute(
                        """
                        SELECT COALESCE(SUM(amount_cents), 0) AS total
                        FROM bills b
                        LEFT JOIN categories c ON c.id = b.app_category_id
                        WHERE b.month_id = ? AND b.direction = 'income'
                          AND COALESCE(c.counts_toward_balance, 1) = 1
                        """,
                        (month_id,),
                    ).fetchone()["total"]
                )
                expense_cents = int(
                    connection.execute(
                        """
                        SELECT COALESCE(SUM(amount_cents), 0) AS total
                        FROM bills WHERE month_id = ? AND direction = 'expense'
                        """,
                        (month_id,),
                    ).fetchone()["total"]
                )
                unclassified_expense_cents = int(
                    connection.execute(
                        """
                        SELECT COALESCE(SUM(amount_cents), 0) AS total
                        FROM bills
                        WHERE month_id = ?
                          AND direction = 'expense'
                          AND app_category_id IS NULL
                        """,
                        (month_id,),
                    ).fetchone()["total"]
                )

        effective_income = countable_income_cents if income_enabled else 0
        available_cents = total_cents + effective_income - expense_cents
        return {
            "month": month,
            "total_balance": from_cents(total_cents),
            "income_total": from_cents(income_cents),
            "transfer_income_total": from_cents(income_cents - countable_income_cents),
            "income_counted": from_cents(effective_income),
            "available_balance": from_cents(available_cents),
            "expense_total": from_cents(expense_cents),
            "unclassified_expense": from_cents(unclassified_expense_cents),
            "income_counts_toward_balance": income_enabled,
        }

