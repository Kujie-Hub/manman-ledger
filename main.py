"""Android Flet 应用入口。"""

from __future__ import annotations

import flet as ft

from frontend.app import main as app_main


async def main(page: ft.Page) -> None:
    await app_main(page)


def run() -> None:
    ft.run(main)


if __name__ == "__main__":
    run()

