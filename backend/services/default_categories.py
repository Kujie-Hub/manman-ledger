"""初始化默认的转账、生活消费和存钱分类。"""

from __future__ import annotations

import sqlite3


DEFAULT_EXPENSE = (
    "转账-转出",
    "吃饭",
    "游乐",
    "购物",
    "小金猪（存钱）",
)

DEFAULT_INCOME = ("转账-转入",)


def ensure_default_categories_for_month(
    connection: sqlite3.Connection,
    month_id: int,
) -> None:
    """为一个月补齐默认分类；已存在的同名分类不覆盖。"""
    for name in DEFAULT_EXPENSE:
        connection.execute(
            """
            INSERT OR IGNORE INTO categories(
                month_id, name, kind, limit_cents, counts_toward_balance
            ) VALUES (?, ?, 'expense', 0, 1)
            """,
            (month_id, name),
        )

    for name in DEFAULT_INCOME:
        connection.execute(
            """
            INSERT OR IGNORE INTO categories(
                month_id, name, kind, limit_cents, counts_toward_balance
            ) VALUES (?, ?, 'income', NULL, 0)
            """,
            (month_id, name),
        )
        connection.execute(
            """
            UPDATE categories
            SET counts_toward_balance = 0
            WHERE month_id = ? AND name = ? AND kind = 'income'
            """,
            (month_id, name),
        )


def ensure_default_categories_for_months(connection: sqlite3.Connection) -> None:
    month_ids = connection.execute("SELECT id FROM months").fetchall()
    for row in month_ids:
        ensure_default_categories_for_month(connection, int(row["id"]))

