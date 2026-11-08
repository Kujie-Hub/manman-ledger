"""把已导入账单批量映射到同方向分类，并在失败时降级为“未分类”。"""

from __future__ import annotations

from backend.db import Database
from backend.services.balance import BalanceService
from backend.services.deepseek import DeepSeekClient, DeepSeekError
from backend.services.prompts import classification_prompt
from backend.services.statements import ParsedBill


RULE_GROUPS = (
    ("小金猪（存钱）", ("存钱", "储蓄", "定期存款", "余额宝", "理财通", "基金定投", "小金猪")),
    (
        "购物",
        ("衣服", "服装", "鞋", "帽", "优衣库", "耐克", "阿迪达斯", "内衣", "裤", "裙"),
    ),
    (
        "购物",
        ("文具", "图书", "书店", "课程", "网课", "培训", "教育", "学习用品", "打印"),
    ),
    (
        "购物",
        (
            "超市",
            "日用",
            "洗护",
            "清洁",
            "纸巾",
            "洗衣液",
            "洗洁精",
            "牙膏",
            "香皂",
            "生活用品",
            "家居",
        ),
    ),
    (
        "吃饭",
        (
            "餐饮",
            "饭店",
            "餐厅",
            "美食",
            "外卖",
            "饿了么",
            "麦当劳",
            "肯德基",
            "星巴克",
            "瑞幸",
            "咖啡",
            "奶茶",
            "烧烤",
            "火锅",
            "面馆",
            "猪脚饭",
            "汤粉",
            "早餐",
            "午餐",
            "晚餐",
            "食堂",
        ),
    ),
    (
        "游乐",
        ("电影", "影城", "游戏", "steam", "网吧", "景区", "旅游", "酒店", "门票", "演唱会", "健身", "ktv", "娱乐"),
    ),
    ("购物", ("淘宝", "天猫", "京东", "拼多多", "抖音电商", "商城", "商场", "百货", "购物")),
)


class BillClassifier:
    def __init__(self, database: Database, client: DeepSeekClient | None = None) -> None:
        self.db = database
        self.client = client or DeepSeekClient()

    def _categories(self, month: str) -> dict[str, list[dict]]:
        with self.db.read() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.name, c.kind, c.parent_id, p.name AS parent_name
                FROM categories c
                JOIN months m ON m.id = c.month_id
                LEFT JOIN categories p ON p.id = c.parent_id
                WHERE m.month = ?
                ORDER BY c.kind, COALESCE(p.name, c.name), c.parent_id IS NOT NULL, c.name
                """,
                (month,),
            ).fetchall()
        result: dict[str, list[dict]] = {"expense": [], "income": []}
        for row in rows:
            result[row["kind"]].append(
                {
                    "id": int(row["id"]),
                    "name": (
                        f"{row['parent_name']} / {row['name']}"
                        if row["parent_name"]
                        else row["name"]
                    ),
                    "kind": row["kind"],
                }
            )
        return result

    async def classify(
        self,
        month: str,
        bills: list[ParsedBill],
    ) -> tuple[list[int | None], str | None]:
        """返回与输入顺序一致的分类 ID；无法分类时返回 None 和提示。"""
        if not bills:
            return [], None
        with self.db.transaction() as connection:
            BalanceService(self.db).ensure_month(connection, month)
        normalized_bills = [
            {
                "description": bill.description,
                "direction": bill.direction,
                "source_category": bill.source_category,
            }
            for bill in bills
        ]
        categories = self._categories(month)
        if not categories["expense"] and not categories["income"]:
            return [None] * len(bills), "当前月份还没有分类，账单已保存为“未分类”"

        allowed_by_direction: dict[str, dict[str, int]] = {}
        for direction, items in categories.items():
            names: dict[str, int] = {}
            for item in items:
                names[item["name"]] = item["id"]
                leaf_name = item["name"].rsplit(" / ", 1)[-1]
                names.setdefault(leaf_name, item["id"])
            allowed_by_direction[direction] = names
        results: list[int | None] = [None] * len(bills)
        uncertain: list[tuple[int, dict]] = []
        for index, bill in enumerate(normalized_bills):
            rule_path = self._rule_path(bill)
            category_id = allowed_by_direction.get(bill["direction"], {}).get(rule_path)
            if category_id is not None:
                results[index] = category_id
            else:
                uncertain.append((index, bill))

        if not uncertain:
            return results, None
        if not self.client.configured:
            return results, (
                f"{len(uncertain)} 笔账单无法根据明确规则判断，已保留为“未分类”；"
                "可在设置中配置 DeepSeek 后重新导入或手动分类"
            )

        warnings: list[str] = []
        try:
            for offset in range(0, len(uncertain), 60):
                batch = uncertain[offset : offset + 60]
                prompt = classification_prompt(
                    month,
                    categories,
                    [bill for _, bill in batch],
                )
                response = await self.client.classify(prompt)
                payload = response.get("classifications", [])
                if not isinstance(payload, list):
                    raise DeepSeekError("分类结果缺少 classifications 数组")
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    try:
                        local_index = int(item.get("index"))
                    except (TypeError, ValueError):
                        continue
                    if not 0 <= local_index < len(batch):
                        continue
                    original_index, bill = batch[local_index]
                    category_name = item.get("category")
                    category_id = allowed_by_direction.get(bill["direction"], {}).get(category_name)
                    results[original_index] = category_id
        except DeepSeekError as exc:
            warnings.append(f"DeepSeek 辅助分类未完成：{exc}")
        unclassified = sum(results[index] is None for index, _ in uncertain)
        if unclassified:
            warnings.append(f"{unclassified} 笔账单无法可靠判断，已保留为“未分类”")
        return results, "；".join(warnings) if warnings else None

    @staticmethod
    def _rule_path(bill: dict) -> str | None:
        text = f"{bill.get('source_category') or ''} {bill.get('description') or ''}".lower()
        is_refund = any(word in text for word in ("退款", "退货", "退还"))
        if bill["direction"] == "income":
            if is_refund or any(word in text for word in ("工资", "奖金", "红包")):
                return None
            if "转账" in text or "转入" in text:
                return "转账-转入"
            return None
        if is_refund:
            return None
        if "转账" in text or "转出" in text:
            return "转账-转出"
        for path, keywords in RULE_GROUPS:
            if any(keyword in text for keyword in keywords):
                return path
        return None


