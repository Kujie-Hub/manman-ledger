"""SQLite 连接、建表与事务辅助，所有金额以“分”为单位保存。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from backend.config import database_path


SCHEMA = """
CREATE TABLE IF NOT EXISTS months (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL UNIQUE,
    total_balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(total_balance_cents >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_id INTEGER NOT NULL REFERENCES months(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('expense', 'income')),
    limit_cents INTEGER CHECK(limit_cents IS NULL OR limit_cents >= 0),
    counts_toward_balance INTEGER NOT NULL DEFAULT 1 CHECK(counts_toward_balance IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(month_id, name, kind),
    CHECK((kind = 'expense' AND limit_cents IS NOT NULL) OR (kind = 'income' AND limit_cents IS NULL))
);

CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_id INTEGER NOT NULL REFERENCES months(id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    direction TEXT NOT NULL CHECK(direction IN ('expense', 'income')),
    bill_type TEXT NOT NULL DEFAULT 'expense'
        CHECK(bill_type IN ('expense', 'income', 'transfer')),
    is_special INTEGER NOT NULL DEFAULT 0 CHECK(is_special IN (0, 1)),
    deduct_from_balance INTEGER NOT NULL DEFAULT 1 CHECK(deduct_from_balance IN (0, 1)),
    affect_balance INTEGER NOT NULL DEFAULT 1 CHECK(affect_balance IN (0, 1)),
    payment_method TEXT NOT NULL DEFAULT '其他',
    is_refunded INTEGER NOT NULL DEFAULT 0 CHECK(is_refunded IN (0, 1)),
    refund_of_bill_id INTEGER REFERENCES bills(id) ON DELETE SET NULL,
    count_in_balance INTEGER NOT NULL DEFAULT 1 CHECK(count_in_balance IN (0, 1)),
    description TEXT NOT NULL,
    source_category TEXT,
    app_category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    source TEXT NOT NULL CHECK(source IN ('manual', 'alipay', 'wechat', 'screenshot')),
    external_id TEXT,
    dedup_key TEXT NOT NULL UNIQUE,
    raw_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_categories_month ON categories(month_id);
CREATE INDEX IF NOT EXISTS idx_bills_month_date ON bills(month_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_bills_category ON bills(app_category_id);
"""


class Database:
    """轻量 SQLite 包装；每个操作使用独立连接并启用 WAL。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 20000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.transaction() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(categories)").fetchall()
            }
            if "parent_id" not in columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN parent_id INTEGER "
                    "REFERENCES categories(id) ON DELETE CASCADE"
                )
            if "counts_toward_balance" not in columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN counts_toward_balance "
                    "INTEGER NOT NULL DEFAULT 1 CHECK(counts_toward_balance IN (0, 1))"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO app_settings(key, value)
                VALUES ('income_counts_toward_balance', 'false')
                """
            )
            bill_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(bills)").fetchall()
            }
            bill_type_added = "bill_type" not in bill_columns
            payment_method_added = "payment_method" not in bill_columns
            affect_balance_added = "affect_balance" not in bill_columns
            bill_migrations = {
                "bill_type": "TEXT NOT NULL DEFAULT 'expense'",
                "is_special": "INTEGER NOT NULL DEFAULT 0",
                "deduct_from_balance": "INTEGER NOT NULL DEFAULT 1",
                "affect_balance": "INTEGER NOT NULL DEFAULT 1",
                "payment_method": "TEXT NOT NULL DEFAULT '其他'",
                "is_refunded": "INTEGER NOT NULL DEFAULT 0",
                "refund_of_bill_id": "INTEGER REFERENCES bills(id) ON DELETE SET NULL",
                "count_in_balance": "INTEGER NOT NULL DEFAULT 1",
            }
            for column, definition in bill_migrations.items():
                if column not in bill_columns:
                    connection.execute(
                        f"ALTER TABLE bills ADD COLUMN {column} {definition}"
                    )
            if bill_type_added:
                connection.execute(
                    """
                    UPDATE bills
                    SET bill_type = CASE
                        WHEN app_category_id IN (
                            SELECT id FROM categories
                            WHERE name IN ('转账-转入', '转账-转出')
                        ) THEN 'transfer'
                        ELSE direction
                    END
                    """
                )
            if payment_method_added:
                connection.execute(
                    """
                    UPDATE bills
                    SET payment_method = CASE source
                        WHEN 'wechat' THEN '微信'
                        WHEN 'alipay' THEN '支付宝'
                        ELSE '其他'
                    END
                    """
                )
            if affect_balance_added:
                connection.execute(
                    """
                    UPDATE bills
                    SET affect_balance = CASE
                        WHEN bill_type = 'income' THEN 0
                        ELSE 1
                    END
                    """
                )
            from backend.services.default_categories import ensure_default_categories_for_months

            ensure_default_categories_for_months(connection)


db = Database()

