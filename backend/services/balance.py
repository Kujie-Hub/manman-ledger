"""月度结算、结转和账单余额口径的唯一计算入口。"""

from __future__ import annotations

from datetime import date, timedelta

from backend.db import Database
from backend.money import from_cents


def previous_month(month: str) -> str:
    year, number = map(int, month.split("-"))
    value = date(year, number, 1)
    return (value - timedelta(days=1)).strftime("%Y-%m")


class BalanceService:
    def __init__(self, database: Database) -> None:
        self.db = database

    def get_income_counts_toward_balance(self) -> bool:
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                ("income_counts_toward_balance",),
            ).fetchone()
        return bool(row and str(row["value"]).lower() == "true")

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

    def apply_income_switch_to_month(self, month: str, enabled: bool) -> None:
        """全局按钮批量控制本月收入与全部转账账单的 affectBalance。"""
        self.set_income_counts_toward_balance(enabled)
        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE bills
                SET affect_balance = ?, updated_at = CURRENT_TIMESTAMP
                WHERE month_id = (SELECT id FROM months WHERE month = ?)
                  AND bill_type IN ('income', 'transfer')
                """,
                (1 if enabled else 0, month),
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
        return self._settle_month(self.validate_month(month), set())

    def _settle_month(self, month: str, visiting: set[str]) -> dict:
        if month in visiting:
            raise ValueError("月份结转出现循环")
        visiting = {*visiting, month}
        prior_month = previous_month(month)
        prior = self._month_row(prior_month)
        prior_has_data = bool(
            prior and (prior["total_balance_cents"] > 0 or prior["bill_count"] > 0)
        )
        prior_summary = (
            self._settle_month(prior_month, visiting) if prior_has_data else None
        )

        current = self._month_row(month)
        initial_cents = int(current["total_balance_cents"]) if current else 0
        opening_cents = (
            prior_summary["closing_balance_cents"]
            if prior_summary
            else initial_cents
        )
        stats = self._bill_stats(month)
        closing_cents = (
            opening_cents
            + stats["income_affect_cents"]
            + stats["transfer_in_affect_cents"]
            - stats["expense_affect_cents"]
            - stats["transfer_out_affect_cents"]
        )
        return {
            "month": month,
            "initial_balance": from_cents(initial_cents),
            "opening_balance": from_cents(opening_cents),
            "opening_balance_cents": opening_cents,
            "available_balance": from_cents(closing_cents),
            "closing_balance": from_cents(closing_cents),
            "closing_balance_cents": closing_cents,
            "previous_month": prior_month if prior_summary else None,
            "previous_closing_balance": (
                from_cents(prior_summary["closing_balance_cents"])
                if prior_summary
                else "0.00"
            ),
            "previous_income_total": (
                from_cents(prior_summary["income_total_cents"])
                if prior_summary
                else "0.00"
            ),
            "has_previous_data": bool(prior_summary),
            "income_total": from_cents(stats["income_cents"]),
            "expense_total": from_cents(stats["expense_cents"]),
            "transfer_in_total": from_cents(stats["transfer_in_cents"]),
            "transfer_out_total": from_cents(stats["transfer_out_cents"]),
            "income_counted": from_cents(stats["income_affect_cents"]),
            "transfer_in_counted": from_cents(stats["transfer_in_affect_cents"]),
            "expense_counted": from_cents(stats["expense_affect_cents"]),
            "transfer_out_counted": from_cents(stats["transfer_out_affect_cents"]),
            "special_total": from_cents(stats["special_cents"]),
            "refunded_transfer_count": stats["refunded_transfer_count"],
            "refunded_transfer_total": from_cents(stats["refunded_transfer_total"]),
            "unclassified_expense": from_cents(stats["unclassified_expense_cents"]),
            "payment_summary": stats["payment_summary"],
            "income_counts_toward_balance": bool(
                stats["income_transfer_count"] > 0
                and stats["income_transfer_count"] == stats["income_transfer_affect_count"]
            ),
            "income_total_cents": stats["income_cents"],
        }

    def _month_row(self, month: str):
        with self.db.read() as connection:
            return connection.execute(
                """
                SELECT m.id, m.month, m.total_balance_cents,
                       (SELECT COUNT(*) FROM bills b WHERE b.month_id = m.id) AS bill_count
                FROM months m
                WHERE m.month = ?
                """,
                (month,),
            ).fetchone()

    def _bill_stats(self, month: str) -> dict:
        with self.db.read() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN b.bill_type = 'income'
                        THEN b.amount_cents ELSE 0 END), 0) AS income_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'expense'
                        THEN b.amount_cents ELSE 0 END), 0) AS expense_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'transfer'
                        AND b.direction = 'income' THEN b.amount_cents ELSE 0 END), 0)
                        AS transfer_in_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'transfer'
                        AND b.direction = 'expense' THEN b.amount_cents ELSE 0 END), 0)
                        AS transfer_out_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'income'
                        AND b.affect_balance = 1
                        THEN b.amount_cents ELSE 0 END), 0) AS income_affect_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'transfer'
                        AND b.direction = 'income' AND b.affect_balance = 1
                        THEN b.amount_cents ELSE 0 END), 0) AS transfer_in_affect_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'expense'
                        AND b.affect_balance = 1 AND b.count_in_balance = 1
                        AND (b.is_special = 0 OR b.deduct_from_balance = 1)
                        THEN b.amount_cents ELSE 0 END), 0) AS expense_affect_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'transfer'
                        AND b.direction = 'expense' AND b.affect_balance = 1
                        AND b.count_in_balance = 1
                        AND (b.is_special = 0 OR b.deduct_from_balance = 1)
                        THEN b.amount_cents ELSE 0 END), 0) AS transfer_out_affect_cents,
                    COALESCE(SUM(CASE WHEN b.is_special = 1
                        THEN b.amount_cents ELSE 0 END), 0) AS special_cents,
                    COUNT(CASE WHEN b.is_refunded = 1 AND b.bill_type = 'transfer'
                        AND b.direction = 'expense' THEN 1 END)
                        AS refunded_transfer_count,
                    COALESCE(SUM(CASE WHEN b.is_refunded = 1 AND b.bill_type = 'transfer'
                        AND b.direction = 'expense' THEN b.amount_cents ELSE 0 END), 0)
                        AS refunded_transfer_total,
                    COUNT(CASE WHEN b.bill_type IN ('income', 'transfer')
                        THEN 1 END) AS income_transfer_count,
                    COUNT(CASE WHEN b.bill_type IN ('income', 'transfer')
                        AND b.affect_balance = 1 THEN 1 END) AS income_transfer_affect_count,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'expense'
                        AND b.app_category_id IS NULL THEN b.amount_cents ELSE 0 END), 0)
                        AS unclassified_expense_cents
                FROM bills b
                JOIN months m ON m.id = b.month_id
                WHERE m.month = ?
                """,
                (month,),
            ).fetchone()
            payment_rows = connection.execute(
                """
                SELECT b.payment_method,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'income'
                        THEN b.amount_cents ELSE 0 END), 0) AS income_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'expense'
                        THEN b.amount_cents ELSE 0 END), 0) AS expense_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'transfer'
                        AND b.direction = 'income' THEN b.amount_cents ELSE 0 END), 0)
                        AS transfer_in_cents,
                    COALESCE(SUM(CASE WHEN b.bill_type = 'transfer'
                        AND b.direction = 'expense' THEN b.amount_cents ELSE 0 END), 0)
                        AS transfer_out_cents
                FROM bills b
                JOIN months m ON m.id = b.month_id
                WHERE m.month = ?
                GROUP BY b.payment_method
                ORDER BY b.payment_method
                """,
                (month,),
            ).fetchall()
        result = {key: int(row[key]) for key in row.keys()}
        result["payment_summary"] = [
            {
                "payment_method": item["payment_method"] or "其他",
                "income": from_cents(item["income_cents"]),
                "expense": from_cents(item["expense_cents"]),
                "transfer_in": from_cents(item["transfer_in_cents"]),
                "transfer_out": from_cents(item["transfer_out_cents"]),
            }
            for item in payment_rows
        ]
        return result

