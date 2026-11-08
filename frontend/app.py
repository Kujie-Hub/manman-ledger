"""Android 本地记账界面，所有操作直接调用本地业务层。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

import flet as ft

from frontend.local_api import ApiError, LocalLedgerGateway


API_KEY_SETTING = "personal_ledger_deepseek_api_key"
ONBOARDING_SETTING = "onboarding_seen_v4"


def now_local() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def shift_month(month: str, offset: int) -> str:
    year, number = map(int, month.split("-"))
    value = date(year, number, 1)
    if offset > 0:
        for _ in range(offset):
            value = (value.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        for _ in range(-offset):
            value = (value - timedelta(days=1)).replace(day=1)
    return value.strftime("%Y-%m")


def money(value: Any) -> str:
    try:
        return f"¥{Decimal(str(value or 0)):,.2f}"
    except InvalidOperation:
        return "¥0.00"


def kind_label(value: str) -> str:
    return "收入分类" if value == "income" else "支出分类"


def type_label(value: str, direction: str) -> str:
    if value == "transfer":
        return "转入" if direction == "income" else "转出"
    return "收入" if value == "income" else "支出"


def source_label(value: str) -> str:
    return {
        "alipay": "支付宝",
        "wechat": "微信",
        "screenshot": "截图",
        "manual": "手动",
    }.get(value, value)


class LedgerUI:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.prefs = ft.SharedPreferences()
        self.picker = ft.FilePicker()
        self.gateway: LocalLedgerGateway | None = None
        self.month = now_local().strftime("%Y-%m")
        self.section = 0
        self.detail_id: int | None = None
        self.show_unclassified = False
        self.payment_filter = "全部"
        self.summary: dict = {}
        self.categories: list[dict] = []
        self.bills: list[dict] = []
        self.status: dict = {}
        self.load_error: str | None = None
        self.body = ft.ListView(expand=True, spacing=14, padding=16)
        self.loading = ft.ProgressRing(width=20, height=20, stroke_width=2, visible=False)

    async def run(self) -> None:
        self._configure_page()
        self.page.add(self.body)
        key = ""
        if not self.page.web:
            try:
                key = str(await self.prefs.get(API_KEY_SETTING) or "")
            except Exception:
                key = ""
        self.gateway = LocalLedgerGateway(key)
        await self.reload()
        await self._maybe_show_onboarding()

    def _configure_page(self) -> None:
        self.page.title = "慢慢记账"
        self.page.bgcolor = "#F5F3EF"
        self.page.padding = 0
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.appbar = ft.AppBar(
            title=ft.Text("慢慢记账", size=20, weight=ft.FontWeight.BOLD),
            bgcolor="#F5F3EF",
            actions=[self.loading],
        )
        self.page.navigation_bar = ft.NavigationBar(
            selected_index=self.section,
            destinations=[
                ft.NavigationBarDestination(icon=ft.Icons.HOME_OUTLINED, selected_icon=ft.Icons.HOME, label="月度"),
                ft.NavigationBarDestination(icon=ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED, selected_icon=ft.Icons.ACCOUNT_BALANCE_WALLET, label="分类"),
                ft.NavigationBarDestination(icon=ft.Icons.UPLOAD_FILE_OUTLINED, selected_icon=ft.Icons.UPLOAD_FILE, label="导入"),
                ft.NavigationBarDestination(icon=ft.Icons.RECEIPT_LONG_OUTLINED, selected_icon=ft.Icons.RECEIPT_LONG, label="账单"),
                ft.NavigationBarDestination(icon=ft.Icons.SETTINGS_OUTLINED, selected_icon=ft.Icons.SETTINGS, label="设置"),
            ],
            on_change=self._on_navigation,
        )

    def notify(self, message: str, error: bool = False) -> None:
        self.page.show_dialog(
            ft.SnackBar(
                content=ft.Text(message),
                bgcolor="#B3261E" if error else "#2F5D50",
                show_close_icon=True,
            )
        )

    async def _maybe_show_onboarding(self) -> None:
        if self.page.web:
            return
        try:
            seen = await self.prefs.get(ONBOARDING_SETTING)
        except Exception:
            seen = None
        if not seen:
            self._show_onboarding()

    def _show_onboarding(self) -> None:
        sections = [
            (
                "月度",
                "左右箭头切换月份，“本月”回到当前月。“本月余额”可手动设置；"
                "有上月数据时也可点击“使用上月结转”。“收入/转入计入余额”会批量设置"
                "本月收入和转账账单。",
            ),
            (
                "分类",
                "支出分类只需设置限额。“已花”自动累计账单，“余额”由限额减去已花得到，"
                "不需要手动分配。",
            ),
            (
                "导入",
                "选择文件可导入支付宝 CSV 或微信 XLSX；选择截图会调用 DeepSeek 识别；"
                "手动记账用于补录单笔账单。重复导入会自动跳过。",
            ),
            (
                "账单",
                "“编辑”可修正类型、方向和支付方式；转账账单可在“余额设置”中调整"
                "“记余额”“记花销”。“特殊关注”把账单放到月度页顶部；"
                "“标记已退还”只会关联已有退款，不会重复新增金额。黄色是转账，红色是退款。",
            ),
            (
                "设置",
                "填写 DeepSeek API Key 才能使用截图识别和模糊账单的智能分类；"
                "“新手引导”可随时重新打开本说明。",
            ),
        ]

        async def close_guide() -> None:
            if not self.page.web:
                try:
                    await self.prefs.set(ONBOARDING_SETTING, True)
                except Exception:
                    pass
            self.page.pop_dialog()

        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("新手引导"),
                modal=True,
                content=ft.Column(
                    [
                        ft.Column(
                            [
                                ft.Text(title, weight=ft.FontWeight.BOLD),
                                ft.Text(text, size=13, color="#716861"),
                            ],
                            spacing=2,
                        )
                        for title, text in sections
                    ],
                    tight=True,
                    spacing=14,
                    scroll=ft.ScrollMode.AUTO,
                ),
                actions=[
                    ft.FilledButton(
                        "知道了",
                        on_click=lambda _: self.page.run_task(close_guide),
                    )
                ],
            )
        )

    async def action(self, handler: Callable[[], Awaitable[Any]]) -> Any | None:
        self.loading.visible = True
        self.page.update()
        try:
            return await handler()
        except ApiError as exc:
            self.notify(str(exc), error=True)
        except Exception as exc:
            self.notify(f"操作失败：{exc}", error=True)
        finally:
            self.loading.visible = False
            self.page.update()
        return None

    async def reload(self) -> None:
        if self.gateway is None:
            return

        async def load() -> None:
            self.summary, self.categories, self.bills, self.status = await asyncio.gather(
                self.gateway.get(f"/months/{self.month}"),
                self.gateway.get(f"/months/{self.month}/categories"),
                self.gateway.get("/bills", {"month": self.month}),
                self.gateway.get("/status"),
            )
            self.load_error = None

        await self.action(load)
        if not self.summary:
            self.load_error = "无法读取本机数据"
        self.render()

    async def reload_data(self) -> None:
        if self.gateway is None:
            return

        async def load() -> None:
            self.summary, self.categories, self.bills = await asyncio.gather(
                self.gateway.get(f"/months/{self.month}"),
                self.gateway.get(f"/months/{self.month}/categories"),
                self.gateway.get("/bills", {"month": self.month}),
            )

        await self.action(load)
        self.render()

    async def _on_navigation(self, event: ft.Event) -> None:
        self.section = int(event.control.selected_index or 0)
        self.detail_id = None
        self.show_unclassified = False
        self.render()

    def open_section(self, section: int) -> None:
        self.section = section
        if self.page.navigation_bar:
            self.page.navigation_bar.selected_index = section
        self.render()
        self.page.update()

    def render(self) -> None:
        if self.load_error:
            self.body.controls = [self._load_error()]
        elif self.section == 0:
            self.body.controls = self._month_view()
        elif self.section == 1:
            self.body.controls = self._category_view()
        elif self.section == 2:
            self.body.controls = self._import_view()
        elif self.section == 3:
            self.body.controls = self._bill_view()
        else:
            self.body.controls = self._settings_view()
        self.body.update()

    def _load_error(self) -> ft.Control:
        return self._panel(
            ft.Column(
                [
                    ft.Icon(ft.Icons.ERROR_OUTLINE, color="#B3261E", size=36),
                    ft.Text("本机数据加载失败", weight=ft.FontWeight.BOLD),
                    ft.Text(self.load_error or ""),
                    ft.FilledButton(
                        "重试",
                        on_click=lambda _: self.page.run_task(self.reload),
                    ),
                ],
                spacing=10,
            )
        )

    def _panel(self, content: ft.Control, bgcolor: str = "#FFFFFF") -> ft.Container:
        return ft.Container(
            content=content,
            padding=16,
            border_radius=16,
            bgcolor=bgcolor,
            border=ft.Border.all(1, "#E8E2DA"),
        )

    def _month_switcher(self) -> ft.Control:
        return ft.Row(
            [
                ft.IconButton(
                    icon=ft.Icons.CHEVRON_LEFT,
                    on_click=lambda _: self.page.run_task(self._change_month, -1),
                ),
                ft.Text(self.month, size=19, weight=ft.FontWeight.BOLD),
                ft.IconButton(
                    icon=ft.Icons.CHEVRON_RIGHT,
                    on_click=lambda _: self.page.run_task(self._change_month, 1),
                ),
                ft.TextButton(
                    "本月",
                    on_click=lambda _: self.page.run_task(
                        self._set_month,
                        now_local().strftime("%Y-%m"),
                    ),
                ),
            ],
            spacing=2,
        )

    async def _change_month(self, offset: int) -> None:
        await self._set_month(shift_month(self.month, offset))

    async def _set_month(self, month: str) -> None:
        self.month = month
        self.detail_id = None
        self.show_unclassified = False
        await self.reload_data()

    def _metric(self, label: str, value: str, color: str) -> ft.Container:
        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(label, size=12, color="#716861"),
                    ft.Text(value, size=21, weight=ft.FontWeight.BOLD, color=color),
                ],
                spacing=3,
            ),
            padding=14,
            width=180,
            border_radius=14,
            bgcolor="#FFFFFF",
            border=ft.Border.all(1, "#E8E2DA"),
        )

    def _month_view(self) -> list[ft.Control]:
        total = ft.TextField(
            label="本月余额",
            value=str(self.summary.get("opening_balance", "0.00")),
            prefix=ft.Text("¥ "),
            width=220,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        income_switch = ft.Switch(
            label="收入/转入计入余额",
            value=bool(self.summary.get("income_counts_toward_balance", False)),
            on_change=lambda event: self.page.run_task(self._income_switch, event.control.value),
        )
        return [
            self._month_switcher(),
            self._previous_month_card(),
            ft.Row(
                [
                    self._metric("可用余额", money(self.summary.get("available_balance")), "#2F5D50"),
                    self._metric("本月支出", money(self.summary.get("expense_total")), "#8C3B2E"),
                    self._metric("本月收入", money(self.summary.get("income_total")), "#245C86"),
                    self._metric("转账", (
                        f"入 {money(self.summary.get('transfer_in_total'))} / "
                        f"出 {money(self.summary.get('transfer_out_total'))}"
                    ), "#7A4E2D"),
                    *(
                        [
                            self._metric(
                                "已退还",
                                f"{self.summary.get('refunded_transfer_count', 0)} 笔 · "
                                f"{money(self.summary.get('refunded_transfer_total'))}",
                                "#B3261E",
                            )
                        ]
                        if self.summary.get("refunded_transfer_count", 0)
                        else []
                    ),
                ],
                wrap=True,
                spacing=10,
                run_spacing=10,
            ),
            self._panel(
                ft.Column(
                    [
                        ft.Row(
                            [
                                total,
                                ft.FilledButton(
                                    "保存",
                                    icon=ft.Icons.SAVE_OUTLINED,
                                    on_click=lambda _: self.page.run_task(
                                        self._save_total,
                                        total.value,
                                    ),
                                ),
                                *(
                                    [
                                        ft.TextButton(
                                            "使用上月结转",
                                            on_click=lambda _: self.page.run_task(
                                                self._use_carryover
                                            ),
                                        )
                                    ]
                                    if self.summary.get("can_use_carryover")
                                    else []
                                ),
                            ],
                            wrap=True,
                            spacing=10,
                        ),
                        ft.Text(
                            "手动余额"
                            if self.summary.get("uses_manual_balance")
                            else "自动结转",
                            size=12,
                            color="#716861",
                        ),
                        income_switch,
                    ],
                    spacing=10,
                )
            ),
            self._special_section(),
            self._payment_summary(),
            ft.Text("分类", size=17, weight=ft.FontWeight.BOLD),
            *self._category_rows(),
        ]

    def _previous_month_card(self) -> ft.Control:
        if self.summary.get("has_previous_data"):
            detail = (
                f"{self.summary['previous_month']} · "
                f"期末 {money(self.summary['previous_closing_balance'])} · "
                f"收入 {money(self.summary['previous_income_total'])}"
            )
        else:
            detail = "无数据"
        return self._panel(
            ft.Row(
                [
                    ft.Text("上月结转", weight=ft.FontWeight.BOLD),
                    ft.Text(detail, color="#716861"),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )
        )

    def _special_section(self) -> ft.Control:
        specials = [bill for bill in self.bills if bill["is_special"]]
        if not specials:
            return ft.Container(height=0)
        rows: list[ft.Control] = [ft.Text("特殊关注", size=17, weight=ft.FontWeight.BOLD)]
        for bill in specials:
            rows.append(
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text(
                                            bill["description"],
                                            weight=ft.FontWeight.BOLD,
                                        ),
                                        ft.Text(
                                            f"{type_label(bill['type'], bill['direction'])} · "
                                            f"{bill['payment_method']}",
                                            size=11,
                                            color="#716861",
                                        ),
                                    ],
                                    expand=True,
                                    spacing=2,
                                ),
                                ft.Text(money(bill["amount"]), weight=ft.FontWeight.BOLD),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.START,
                        ),
                    ],
                    spacing=2,
                )
            )
        return self._panel(ft.Column(rows, spacing=8))

    def _payment_summary(self) -> ft.Control:
        rows = self.summary.get("payment_summary") or []
        if not rows:
            return ft.Container(height=0)
        controls: list[ft.Control] = [
            ft.Text("按支付方式", size=17, weight=ft.FontWeight.BOLD)
        ]
        for item in rows:
            controls.append(
                ft.Row(
                    [
                        ft.Text(item["payment_method"], weight=ft.FontWeight.BOLD),
                        ft.Text(
                            f"收入 {money(item['income'])} · 支出 {money(item['expense'])}"
                            + (
                                f" · 转入 {money(item['transfer_in'])}"
                                f" / 转出 {money(item['transfer_out'])}"
                                if item["transfer_in"] != "0.00"
                                or item["transfer_out"] != "0.00"
                                else ""
                            ),
                            size=12,
                            color="#716861",
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    wrap=True,
                )
            )
        return self._panel(ft.Column(controls, spacing=7))

    def _category_rows(self) -> list[ft.Control]:
        if not self.categories:
            return [ft.Text("暂无分类")]
        controls: list[ft.Control] = []
        for category in self.categories:
            if category["name"].startswith("转账-"):
                detail = f"流转 {money(category['transfer_actual'])}"
            elif category["kind"] == "expense":
                detail = (
                    f"限额 {money(category['limit'])}  ·  "
                    f"已花 {money(category['actual'])}  ·  "
                    f"余额 {money(category['remaining'])}"
                )
            else:
                detail = f"累计收入 {money(category['actual'])}"
            controls.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text(category["name"], weight=ft.FontWeight.BOLD),
                                    ft.Text(detail, size=12, color="#716861"),
                                ],
                                expand=True,
                                spacing=3,
                            ),
                            ft.Icon(ft.Icons.CHEVRON_RIGHT, color="#8A817A"),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=16,
                    border_radius=16,
                    bgcolor="#FFFFFF",
                    border=ft.Border.all(1, "#E8E2DA"),
                    on_click=lambda _, category_id=category["id"]: self._open_category(category_id),
                )
            )
        return controls

    def _open_category(self, category_id: int) -> None:
        self.detail_id = category_id
        self.show_unclassified = False
        self.open_section(1)

    async def _save_total(self, value: str) -> None:
        if self.gateway is None:
            return
        try:
            Decimal(value)
        except InvalidOperation:
            self.notify("金额格式不正确", error=True)
            return

        async def save() -> None:
            self.summary = await self.gateway.put(
                f"/months/{self.month}",
                {"total_balance": value},
            )
            await self.reload_data()

        if await self.action(save) is not None:
            self.notify("已保存")

    async def _use_carryover(self) -> None:
        if self.gateway is None:
            return

        async def save() -> None:
            self.summary = await self.gateway.put(
                f"/months/{self.month}",
                {
                    "total_balance": self.summary.get("opening_balance", "0.00"),
                    "use_carryover": True,
                },
            )
            await self.reload_data()

        if await self.action(save) is not None:
            self.notify("已使用上月结转")

    async def _income_switch(self, enabled: bool) -> None:
        if self.gateway is None:
            return

        async def save() -> dict:
            result = await self.gateway.patch(
                "/settings/income-counting",
                {
                    "income_counts_toward_balance": enabled,
                    "month": self.month,
                },
            )
            self.summary = result["summary"]
            return result

        result = await self.action(save)
        if result:
            await self.reload_data()

    def _category_view(self) -> list[ft.Control]:
        if self.detail_id is not None or self.show_unclassified:
            return self._category_detail()

        name = ft.TextField(label="分类名称", width=210)
        kind = ft.Dropdown(
            label="类型",
            value="expense",
            width=150,
            options=[
                ft.DropdownOption(key="expense", text="支出"),
                ft.DropdownOption(key="income", text="收入"),
            ],
        )
        limit = ft.TextField(
            label="限额",
            prefix=ft.Text("¥ "),
            width=160,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        return [
            self._month_switcher(),
            self._panel(
                ft.Column(
                    [
                        ft.Row([name, kind, limit], wrap=True, spacing=10),
                        ft.FilledButton(
                            "新增分类",
                            icon=ft.Icons.ADD,
                            on_click=lambda _: self.page.run_task(
                                self._create_category,
                                name.value,
                                kind.value,
                                limit.value,
                            ),
                        ),
                    ],
                    spacing=10,
                )
            ),
            *[self._category_card(item) for item in self.categories],
        ]

    def _category_card(self, category: dict) -> ft.Control:
        if category["name"].startswith("转账-"):
            line = f"流转 {money(category['transfer_actual'])}"
        elif category["kind"] == "expense":
            line = (
                f"限额 {money(category['limit'])}  ·  "
                f"已花 {money(category['actual'])}  ·  "
                f"余额 {money(category['remaining'])}"
            )
        else:
            line = f"累计 {money(category['actual'])}"
        return self._panel(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(category["name"], size=17, weight=ft.FontWeight.BOLD),
                            ft.Text(kind_label(category["kind"]), size=11, color="#716861"),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Text(line, color="#716861"),
                    ft.Row(
                        [
                            ft.TextButton(
                                "编辑",
                                on_click=lambda _, item=category: self._edit_category(item),
                            ),
                            ft.TextButton(
                                f"{category['bill_count']} 笔",
                                on_click=lambda _, category_id=category["id"]: self._open_category(
                                    category_id
                                ),
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ],
                spacing=7,
            )
        )

    def _category_detail(self) -> list[ft.Control]:
        if self.show_unclassified:
            title = "未分类"
            items = [bill for bill in self.bills if bill["category_id"] is None]
        else:
            category = next(
                (item for item in self.categories if item["id"] == self.detail_id),
                None,
            )
            if category is None:
                self.detail_id = None
                return self._category_view()
            title = category["name"]
            items = [bill for bill in self.bills if bill["category_id"] == category["id"]]
        return [
            ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        on_click=lambda _: self._close_detail(),
                    ),
                    ft.Text(title, size=19, weight=ft.FontWeight.BOLD),
                ]
            ),
            self._month_switcher(),
            *self._bill_cards(items, compact=True),
        ]

    def _close_detail(self) -> None:
        self.detail_id = None
        self.show_unclassified = False
        self.render()

    async def _create_category(self, name: str, kind: str, limit: str) -> None:
        if self.gateway is None:
            return
        payload: dict[str, Any] = {"name": (name or "").strip(), "kind": kind}
        if not payload["name"]:
            self.notify("请输入分类名称", error=True)
            return
        if kind == "expense":
            if not limit:
                self.notify("请输入限额", error=True)
                return
            payload["limit"] = limit

        async def create() -> None:
            await self.gateway.post(f"/months/{self.month}/categories", payload)
            await self.reload_data()

        if await self.action(create) is not None:
            self.notify("分类已新增")

    def _edit_category(self, category: dict) -> None:
        name = ft.TextField(label="分类名称", value=category["name"])
        controls: list[ft.Control] = [name]
        limit: ft.TextField | None = None
        if category["kind"] == "expense":
            limit = ft.TextField(
                label="限额",
                value=category["limit"],
                prefix=ft.Text("¥ "),
                keyboard_type=ft.KeyboardType.NUMBER,
            )
            controls.append(limit)

        async def save() -> None:
            if self.gateway is None:
                return
            payload: dict[str, Any] = {"name": name.value}
            if limit is not None:
                payload["limit"] = limit.value
            try:
                await self.gateway.patch(
                    f"/months/{self.month}/categories/{category['id']}",
                    payload,
                )
                self.page.pop_dialog()
                await self.reload_data()
            except ApiError as exc:
                self.notify(str(exc), error=True)

        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("编辑分类"),
                content=ft.Column(controls, tight=True, spacing=10),
                actions=[
                    ft.TextButton("取消", on_click=lambda _: self.page.pop_dialog()),
                    ft.FilledButton("保存", on_click=lambda _: self.page.run_task(save)),
                ],
            )
        )

    def _import_view(self) -> list[ft.Control]:
        manual_date = ft.TextField(
            label="日期",
            value=now_local().strftime("%Y-%m-%d"),
            width=150,
        )
        manual_amount = ft.TextField(
            label="金额",
            prefix=ft.Text("¥ "),
            width=130,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        manual_type = ft.Dropdown(
            label="类型",
            value="expense",
            width=120,
            options=[
                ft.DropdownOption(key="expense", text="支出"),
                ft.DropdownOption(key="income", text="收入"),
                ft.DropdownOption(key="transfer", text="转账"),
            ],
        )
        manual_direction = ft.Dropdown(
            label="方向",
            value="expense",
            width=110,
            options=[
                ft.DropdownOption(key="expense", text="转出/支出"),
                ft.DropdownOption(key="income", text="转入/收入"),
            ],
        )
        manual_payment = ft.Dropdown(
            label="支付方式",
            value=(self.status.get("payment_methods") or ["其他"])[0],
            width=140,
            options=[
                ft.DropdownOption(key=item, text=item)
                for item in self.status.get("payment_methods", ["其他"])
            ],
        )
        manual_description = ft.TextField(label="说明", width=260)
        ai_state = "已连接" if self.status.get("ai_configured") else "未配置"
        return [
            self._panel(
                ft.Column(
                    [
                        ft.Text("账单文件", size=17, weight=ft.FontWeight.BOLD),
                        ft.FilledButton(
                            "选择文件",
                            icon=ft.Icons.FOLDER_OPEN,
                            on_click=lambda _: self.page.run_task(self._pick_statement),
                        ),
                    ],
                    spacing=10,
                )
            ),
            self._panel(
                ft.Column(
                    [
                        ft.Text("截图", size=17, weight=ft.FontWeight.BOLD),
                        ft.FilledTonalButton(
                            "选择截图",
                            icon=ft.Icons.IMAGE_OUTLINED,
                            on_click=lambda _: self.page.run_task(self._pick_screenshot),
                        ),
                        ft.Text(f"DeepSeek：{ai_state}", size=12, color="#716861"),
                    ],
                    spacing=10,
                )
            ),
            self._panel(
                ft.Column(
                    [
                        ft.Text("手动记账", size=17, weight=ft.FontWeight.BOLD),
                        ft.Row(
                            [
                                manual_date,
                                manual_amount,
                                manual_type,
                                manual_direction,
                                manual_payment,
                                manual_description,
                            ],
                            wrap=True,
                            spacing=10,
                        ),
                        ft.FilledButton(
                            "保存",
                            icon=ft.Icons.ADD,
                            on_click=lambda _: self.page.run_task(
                                self._manual_bill,
                                manual_date.value,
                                manual_amount.value,
                                manual_type.value,
                                manual_direction.value,
                                manual_payment.value,
                                manual_description.value,
                            ),
                        ),
                    ],
                    spacing=10,
                )
            ),
        ]

    async def _pick_statement(self) -> None:
        files = await self.picker.pick_files(
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["csv", "xlsx", "xlsm"],
            with_data=True,
        )
        if not files or self.gateway is None:
            return
        selected = files[0]
        data = self._file_bytes(selected)
        if data is None:
            self.notify("无法读取文件", error=True)
            return

        async def upload() -> dict:
            result = await self.gateway.upload("/imports/statement", selected.name, data)
            await self.reload_data()
            return result

        result = await self.action(upload)
        if result:
            self._notify_import(result)
            self.open_section(3)

    async def _pick_screenshot(self) -> None:
        files = await self.picker.pick_files(
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["jpg", "jpeg", "png", "gif", "webp"],
            with_data=True,
        )
        if not files or self.gateway is None:
            return
        selected = files[0]
        data = self._file_bytes(selected)
        if data is None:
            self.notify("无法读取截图", error=True)
            return

        async def upload() -> dict:
            result = await self.gateway.upload("/imports/screenshot", selected.name, data)
            await self.reload_data()
            return result

        result = await self.action(upload)
        if result:
            self._notify_import(result)
            self.open_section(3)

    @staticmethod
    def _file_bytes(file: ft.FilePickerFile) -> bytes | None:
        if file.bytes is not None:
            return file.bytes
        if file.path:
            try:
                with open(file.path, "rb") as source:
                    return source.read()
            except OSError:
                return None
        return None

    def _notify_import(self, result: dict) -> None:
        parts = [
            f"新增 {result.get('imported', 0)}",
            f"重复 {result.get('duplicates', 0)}",
            f"忽略 {result.get('ignored', 0)}",
            f"失败 {result.get('failed', 0)}",
        ]
        if result.get("warning"):
            parts.append(result["warning"])
        self.notify("；".join(parts))

    async def _manual_bill(
        self,
        occurred_at: str,
        amount: str,
        bill_type: str,
        direction: str,
        payment_method: str,
        description: str,
    ) -> None:
        if self.gateway is None:
            return
        try:
            datetime.strptime(occurred_at, "%Y-%m-%d")
            Decimal(amount)
        except (ValueError, InvalidOperation):
            self.notify("日期或金额格式不正确", error=True)
            return
        if not description.strip():
            self.notify("请输入说明", error=True)
            return
        if bill_type != "transfer":
            direction = bill_type

        async def save() -> dict:
            result = await self.gateway.post(
                "/bills",
                {
                    "occurred_at": occurred_at + "T00:00:00",
                    "amount": amount,
                    "type": bill_type,
                    "direction": direction,
                    "payment_method": payment_method,
                    "description": description.strip(),
                },
            )
            await self.reload_data()
            return result

        result = await self.action(save)
        if result:
            self.notify("已保存")

    def _bill_view(self) -> list[ft.Control]:
        methods = ["全部", *(self.status.get("payment_methods") or [])]
        if self.payment_filter not in methods:
            self.payment_filter = "全部"
        payment_filter = ft.Dropdown(
            label="支付方式",
            value=self.payment_filter,
            width=150,
            options=[ft.DropdownOption(key=item, text=item) for item in methods],
            on_select=lambda event: self._set_payment_filter(event.control.value),
        )
        if self.show_unclassified:
            items = [bill for bill in self.bills if bill["category_id"] is None]
            heading = ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        on_click=lambda _: self._close_detail(),
                    ),
                    ft.Text("未分类", size=19, weight=ft.FontWeight.BOLD),
                ]
            )
        else:
            items = (
                self.bills
                if self.payment_filter == "全部"
                else [
                    bill
                    for bill in self.bills
                    if bill["payment_method"] == self.payment_filter
                ]
            )
            heading = ft.Row(
                [
                    ft.Text(f"{len(items)} 笔", color="#716861"),
                    payment_filter,
                    ft.TextButton(
                        "未分类",
                        on_click=lambda _: self._open_unclassified(),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )
        return [self._month_switcher(), heading, *self._bill_cards(items)]

    def _set_payment_filter(self, value: str | None) -> None:
        self.payment_filter = value or "全部"
        self.render()

    def _open_unclassified(self) -> None:
        self.show_unclassified = True
        self.detail_id = None
        self.render()

    def _bill_cards(self, bills: list[dict], compact: bool = False) -> list[ft.Control]:
        if not bills:
            return [self._panel(ft.Text("暂无账单", color="#716861"))]
        cards: list[ft.Control] = []
        for bill in bills:
            categories = [item for item in self.categories if item["kind"] == bill["direction"]]
            options = [ft.DropdownOption(key="0", text="未分类")]
            options.extend(ft.DropdownOption(key=str(item["id"]), text=item["name"]) for item in categories)
            category = ft.Dropdown(
                label="分类",
                value=str(bill["category_id"] or 0),
                options=options,
                width=160,
                dense=True,
            )
            if not compact:
                category.on_select = lambda event, bill_id=bill["id"]: self.page.run_task(
                    self._set_bill_category,
                    bill_id,
                    event.control.value,
                )
            sign = "−" if bill["direction"] == "expense" else "+"
            color = "#8C3B2E" if bill["direction"] == "expense" else "#245C86"
            badges = [
                ft.Text(type_label(bill["type"], bill["direction"]), size=11, color=color),
                ft.Text(bill["payment_method"], size=11, color="#716861"),
            ]
            if bill["is_special"]:
                badges.append(ft.Text("特殊关注", size=11, color="#8A5A16"))
            if bill["type"] == "transfer" and not bill["affect_balance"]:
                badges.append(ft.Text("不计余额", size=11, color="#B3261E"))
            if (
                bill["type"] == "transfer"
                and bill["direction"] == "expense"
                and not bill["count_in_balance"]
            ):
                badges.append(ft.Text("不计花销", size=11, color="#B3261E"))
            if bill["is_refunded"]:
                badges.append(ft.Text("已退还", size=11, color="#B3261E"))
            if bill["refund_of_bill_id"]:
                badges.append(ft.Text("退款转入", size=11, color="#245C86"))
            special_toggle = ft.Checkbox(
                label="特殊关注",
                value=bill["is_special"],
                on_change=lambda event, bill_id=bill["id"]: self.page.run_task(
                    self._patch_bill,
                    bill_id,
                    {"is_special": event.control.value},
                ),
            )
            highlighted = (
                "#FDECEA"
                if bill["is_refunded"] or bill["refund_of_bill_id"]
                else ("#FFF5E8" if bill["type"] == "transfer" else "#FFFFFF")
            )
            refund_original_id = (
                int(bill["refund_of_bill_id"])
                if bill["refund_of_bill_id"]
                else None
            )
            cards.append(
                self._panel(
                    ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text(
                                                bill["description"],
                                                weight=ft.FontWeight.BOLD,
                                            ),
                                            ft.Text(
                                                f"{bill['occurred_at'][:16]} · "
                                                f"{source_label(bill['source'])}",
                                                size=11,
                                                color="#716861",
                                            ),
                                            ft.Row(badges, spacing=8, wrap=True),
                                        ],
                                        expand=True,
                                        spacing=3,
                                    ),
                                    ft.Column(
                                        [
                                            ft.Text(
                                                f"{sign}{money(bill['amount'])}",
                                                color=color,
                                                weight=ft.FontWeight.BOLD,
                                            ),
                                            (
                                                category
                                                if not compact
                                                else ft.Text(
                                                    bill["category_name"],
                                                    size=11,
                                                )
                                            ),
                                        ],
                                        horizontal_alignment=ft.CrossAxisAlignment.END,
                                        spacing=3,
                                    ),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.START,
                            ),
                            ft.Row(
                                [
                                    special_toggle,
                                    ft.TextButton(
                                        "编辑",
                                        on_click=lambda _, item=bill: self._edit_bill(item),
                                    ),
                                    *(
                                        [
                                            ft.TextButton(
                                                "标记已退还",
                                                icon=ft.Icons.UNDO,
                                                on_click=lambda _, bill_id=bill["id"]: self.page.run_task(
                                                    self._mark_refunded,
                                                    bill_id,
                                                ),
                                            )
                                        ]
                                        if bill["type"] == "transfer"
                                        and bill["direction"] == "expense"
                                        and not bill["is_refunded"]
                                        else []
                                    ),
                                    *(
                                        [
                                            ft.TextButton(
                                                "撤销退还",
                                                icon=ft.Icons.CLOSE,
                                                on_click=lambda _, original_id=(
                                                    bill["id"]
                                                    if bill["is_refunded"]
                                                    else refund_original_id
                                                ): self.page.run_task(
                                                    self._undo_refund,
                                                    original_id,
                                                ),
                                            )
                                        ]
                                        if bill["is_refunded"]
                                        or refund_original_id is not None
                                        else []
                                    ),
                                ],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                wrap=True,
                            ),
                        ],
                        spacing=6,
                    ),
                    bgcolor=highlighted,
                )
            )
        return cards

    async def _set_bill_category(self, bill_id: int, value: str | None) -> None:
        if self.gateway is None:
            return

        async def save() -> None:
            await self.gateway.patch(
                f"/bills/{bill_id}",
                {"category_id": int(value) if value and value != "0" else None},
            )
            await self.reload_data()

        await self.action(save)

    async def _patch_bill(self, bill_id: int, payload: dict) -> None:
        if self.gateway is None:
            return

        async def save() -> None:
            await self.gateway.patch(f"/bills/{bill_id}", payload)
            await self.reload_data()

        await self.action(save)

    def _edit_bill(self, bill: dict) -> None:
        type_field = ft.Dropdown(
            label="类型",
            value=bill["type"],
            options=[
                ft.DropdownOption(key="expense", text="支出"),
                ft.DropdownOption(key="income", text="收入"),
                ft.DropdownOption(key="transfer", text="转账"),
            ],
        )
        direction_field = ft.Dropdown(
            label="方向",
            value=bill["direction"],
            options=[
                ft.DropdownOption(key="expense", text="支出/转出"),
                ft.DropdownOption(key="income", text="收入/转入"),
            ],
        )
        payment_field = ft.Dropdown(
            label="支付方式",
            value=bill["payment_method"],
            options=[
                ft.DropdownOption(key=item, text=item)
                for item in self.status.get("payment_methods", ["其他"])
            ],
        )
        affect = ft.Switch(label="记余额", value=bill["affect_balance"])
        balance_controls: list[ft.Control] = [affect]
        count: ft.Switch | None = None
        if bill["direction"] == "expense":
            count = ft.Switch(label="记花销", value=bill["count_in_balance"])
            balance_controls.append(count)
        deduct: ft.Switch | None = None
        if bill["is_special"]:
            deduct = ft.Switch(
                label="特殊关注账单从余额扣除",
                value=bill["deduct_from_balance"],
            )
            balance_controls.append(deduct)
        balance_section = (
            ft.ExpansionTile(
                title="余额设置",
                controls=balance_controls,
                dense=True,
                maintain_state=True,
                tile_padding=0,
                controls_padding=ft.Padding.only(top=4),
            )
            if bill["type"] == "transfer"
            else None
        )

        async def save() -> None:
            if self.gateway is None:
                return
            payload = {
                "type": type_field.value,
                "direction": direction_field.value,
                "payment_method": payment_field.value,
                "affect_balance": affect.value,
            }
            if count is not None:
                payload["count_in_balance"] = count.value
            if deduct is not None:
                payload["deduct_from_balance"] = deduct.value
            try:
                await self.gateway.patch(f"/bills/{bill['id']}", payload)
                self.page.pop_dialog()
                await self.reload_data()
            except ApiError as exc:
                self.notify(str(exc), error=True)

        self.page.show_dialog(
            ft.AlertDialog(
                title=ft.Text("编辑账单"),
                content=ft.Column(
                    [
                        type_field,
                        direction_field,
                        payment_field,
                        *([balance_section] if balance_section is not None else []),
                    ],
                    tight=True,
                    spacing=8,
                    scroll=ft.ScrollMode.AUTO,
                ),
                actions=[
                    ft.TextButton("取消", on_click=lambda _: self.page.pop_dialog()),
                    ft.FilledButton("保存", on_click=lambda _: self.page.run_task(save)),
                ],
            )
        )

    async def _mark_refunded(self, bill_id: int) -> None:
        if self.gateway is None:
            return

        async def save() -> dict:
            result = await self.gateway.post(f"/bills/{bill_id}/refund", {})
            await self.reload_data()
            return result

        result = await self.action(save)
        if result:
            self.notify(
                "已关联账单中已有的退款转入"
                if result.get("refund")
                else "已标记退还；未找到对应退款转入，请手动补录"
            )

    async def _undo_refund(self, original_id: int) -> None:
        if self.gateway is None:
            return

        async def save() -> dict:
            result = await self.gateway.post(
                f"/bills/{original_id}/refund/undo",
                {},
            )
            await self.reload_data()
            return result

        if await self.action(save) is not None:
            self.notify("已撤销退还标记")

    def _settings_view(self) -> list[ft.Control]:
        key = ft.TextField(
            label="DeepSeek API Key",
            hint_text="已保存时留空",
            password=True,
            can_reveal_password=True,
            width=360,
        )
        configured = bool(self.status.get("ai_configured"))
        return [
            self._panel(
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.CHECK_CIRCLE if configured else ft.Icons.INFO_OUTLINE,
                                    color="#2F5D50" if configured else "#9A6B2F",
                                ),
                                ft.Text("DeepSeek", weight=ft.FontWeight.BOLD),
                            ],
                            spacing=8,
                        ),
                        key,
                        ft.FilledButton(
                            "保存",
                            icon=ft.Icons.SAVE_OUTLINED,
                            on_click=lambda _: self.page.run_task(self._save_key, key.value),
                        ),
                        ft.TextButton(
                            "新手引导",
                            icon=ft.Icons.HELP_OUTLINE,
                            on_click=lambda _: self._show_onboarding(),
                        ),
                    ],
                    spacing=10,
                )
            )
        ]

    async def _save_key(self, value: str) -> None:
        key = (value or "").strip()
        if not key:
            self.notify("请输入 API Key", error=True)
            return
        if self.gateway is None:
            return
        if not self.page.web:
            try:
                await self.prefs.set(API_KEY_SETTING, key)
            except Exception:
                self.notify("API Key 无法保存", error=True)
                return
        self.gateway.set_deepseek_key(key)
        self.status = self.gateway.application.status()
        self.render()
        self.notify("已保存")


async def main(page: ft.Page) -> None:
    await LedgerUI(page).run()
