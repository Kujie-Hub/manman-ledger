"""读取手机应用私有数据路径和 DeepSeek 连接参数。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent
RESOURCE_ROOT = SOURCE_ROOT


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout_seconds: float = 90


def load_deepseek_config() -> DeepSeekConfig:
    """Key 由 Android 设置页注入，请求地址和模型保留可测试的环境变量覆盖。"""
    return DeepSeekConfig(
        api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        base_url=os.getenv(
            "DEEPSEEK_BASE_URL",
            "https://api.deepseek.com",
        ).rstrip("/"),
        model=os.getenv(
            "DEEPSEEK_MODEL",
            "deepseek-flash",
        ).strip(),
        timeout_seconds=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", 90)),
    )


def database_path() -> Path:
    configured = os.getenv("PERSONAL_LEDGER_DB_PATH")
    storage_root = os.getenv("FLET_APP_STORAGE_DATA", "").strip()
    root = Path(storage_root) if storage_root else SOURCE_ROOT
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = root / path
        return path
    return root / "data.db"

