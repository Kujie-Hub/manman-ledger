"""DeepSeek 提示词。集中在此模块以便单独迭代。

官方依据：
- https://api-docs.deepseek.com/guides/vision
- https://api-docs.deepseek.com/guides/json_mode
"""

from __future__ import annotations

import json


SCREENSHOT_SYSTEM_PROMPT = """你是个人记账账单识别助手。请只提取截图中清晰可辨的交易记录，并输出 JSON。
要求：
1. 只输出一个 JSON 对象，格式为：
{"transactions":[{"date":"YYYY-MM-DD HH:MM:SS","amount":"12.34","direction":"支出","description":"商品或交易说明"}]}
2. direction 只能是“支出”或“收入”；无法确定时填 null。
3. 日期无法确定时填 null，金额无法确定时填 null，描述无法确定时填 null。
4. 不要推测、补全或编造截图中没有的信息。
5. 不要输出 Markdown 代码块或任何 JSON 以外的文字。"""


def classification_prompt(month: str, categories: dict[str, list[dict]], bills: list[dict]) -> str:
    category_payload = {
        "expense": [
            {"name": item["name"], "kind": item["kind"]}
            for item in categories.get("expense", [])
        ],
        "income": [
            {"name": item["name"], "kind": item["kind"]}
            for item in categories.get("income", [])
        ],
    }
    bill_payload = [
        {
            "index": index,
            "description": bill["description"],
            "source_category": bill.get("source_category"),
            "direction": bill["direction"],
        }
        for index, bill in enumerate(bills)
    ]
    return f"""请为 {month} 的个人账单逐笔匹配分类，并输出 JSON。
候选分类如下：
{json.dumps(category_payload, ensure_ascii=False)}

账单如下：
{json.dumps(bill_payload, ensure_ascii=False)}

规则：
1. 支出账单只能匹配 kind=expense 的分类；收入账单只能匹配 kind=income 的分类。
2. 结合商品描述、商户名称、原始分类、金额和生活常识判断，例如餐饮店通常是“吃饭”，电影院/游戏/旅游通常是“游乐”，服装、文具、书籍、课程、超市日用品和清洁用品都归入“购物”，存钱/储蓄/定期转入通常是“小金猪（存钱）”。
3. 付款给他人、银行卡或账户转出通常匹配“转账-转出”；明确是转入本人的款项通常匹配“转账-转入”。不要仅因为出现“转账”二字就忽略商品描述。
4. 只在证据充分时分类；描述含糊、可能是退款、借贷、代付或无法确定真实用途时输出“未分类”，不要猜测。
5. 分类名必须逐字使用候选列表中的 name。没有足够依据或没有可用分类时输出“未分类”。
6. 必须为每笔账单返回一项，不要遗漏。
7. 只输出一个 JSON 对象，格式：
{{"classifications":[{{"index":0,"category":"分类名"}}]}}
8. 不要输出 Markdown 代码块或任何 JSON 以外的文字。"""

