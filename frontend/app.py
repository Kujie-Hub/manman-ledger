"""Android 本地记账界面，所有操作直接调用本地业务层。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

import flet as ft

from frontend.local_api import ApiError, LocalLedgerGateway


API_KEY_SETTING = "personal_ledger_deepseek_api_key"


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
            label="总余额",
            value=str(self.summary.get("total_balance", "0.00")),
            prefix=ft.Text("¥ "),
            width=220,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        income_switch = ft.Switch(
            label="收入计入余额",
            value=bool(self.summary.get("income_counts_toward_balance", False)),
            on_change=lambda event: self.page.run_task(self._income_switch, event.control.value),
        )
        return [
            self._month_switcher(),
            ft.Row(
                [
                    self._metric("可用余额", money(self.summary.get("available_balance")), "#2F5D50"),
                    self._metric("本月支出", money(self.summary.get("expense_total")), "#8C3B2E"),
                    self._metric("本月收入", money(self.summary.get("income_total")), "#245C86"),
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
                            ],
                            wrap=True,
                            spacing=10,
                        ),
                        income_switch,
                    ],
                    spacing=10,
                )
            ),
            ft.Text("分类", size=17, weight=ft.FontWeight.BOLD),
            *self._category_rows(),
        ]

    def _category_rows(self) -> list[ft.Control]:
        if not self.categories:
            return [ft.Text("暂无分类")]
        controls: list[ft.Control] = []
        for category in self.categories:
            if category["kind"] == "expense":
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
        if category["kind"] == "expense":
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
        manual_direction = ft.Dropdown(
            label="类型",
            value="expense",
            width=110,
            options=[
                ft.DropdownOption(key="expense", text="支出"),
                ft.DropdownOption(key="income", text="收入"),
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
                            [manual_date, manual_amount, manual_direction, manual_description],
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
                                manual_direction.value,
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
        direction: str,
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

        async def save() -> dict:
            result = await self.gateway.post(
                "/bills",
                {
                    "occurred_at": occurred_at + "T00:00:00",
                    "amount": amount,
                    "direction": direction,
                    "description": description.strip(),
                },
            )
            await self.reload_data()
            return result

        result = await self.action(save)
        if result:
            self.notify("已保存")

    def _bill_view(self) -> list[ft.Control]:
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
            items = self.bills
            heading = ft.Row(
                [
                    ft.Text(f"{len(items)} 笔", color="#716861"),
                    ft.TextButton(
                        "未分类",
                        on_click=lambda _: self._open_unclassified(),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )
        return [self._month_switcher(), heading, *self._bill_cards(items)]

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
            cards.append(
                self._panel(
                    ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Text(bill["description"], weight=ft.FontWeight.BOLD),
                                    ft.Text(
                                        f"{bill['occurred_at'][:16]} · {source_label(bill['source'])}",
                                        size=11,
                                        color="#716861",
                                    ),
                                ],
                                expand=True,
                                spacing=2,
                            ),
                            ft.Column(
                                [
                                    ft.Text(
                                        f"{sign}{money(bill['amount'])}",
                                        color=color,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                    category if not compact else ft.Text(bill["category_name"], size=11),
                                ],
                                horizontal_alignment=ft.CrossAxisAlignment.END,
                                spacing=3,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    )
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
