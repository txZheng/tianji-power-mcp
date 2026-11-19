"""配置与凭据加载。

规则：只用当前工作区里的文件，不依赖任何外部目录。
优先级：环境变量 > 凭据文件 > 报错。

凭据文件名固定为 `.tjweather-rc`（避免与其它项目的 `.env` 撞车）。
也可以用环境变量 `TJW_CREDENTIAL_FILE` 指定其它路径。
为兼容旧安装，找不到 `.tjweather-rc` 时仍会读 `.env`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.tjweather.com/v2"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

CREDENTIAL_FILE = ".tjweather-rc"
# 旧版本用的文件名，仅在没有 .tjweather-rc 时兜底
LEGACY_CREDENTIAL_FILES = (".env",)

_KEY_NAMES = ("TJW_API_KEY", "TIANJI_API_KEY")
_SUB_NAMES = ("TJW_SUBSCRIPTION_ID", "real_sub_id", "TIANJI_SUBSCRIPTION_ID")

# 进程级调用上限：额度有限，默认 20 次/进程，超了直接抛错而不是静默烧额度。
DEFAULT_MAX_CALLS = int(os.environ.get("TJW_MAX_CALLS", "20"))


def _candidate_files() -> list[Path]:
    override = os.environ.get("TJW_CREDENTIAL_FILE")
    if override:
        return [Path(override).expanduser()]
    candidates: list[Path] = []
    for base in (PROJECT_ROOT, Path.cwd()):
        for name in (CREDENTIAL_FILE, *LEGACY_CREDENTIAL_FILES):
            candidates.append(base / name)
    return candidates


def _load_dotenv() -> None:
    """把凭据文件读进 os.environ（不覆盖已存在的环境变量）。"""
    for candidate in _candidate_files():
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        return


def _first(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class Settings:
    api_key: str
    subscription_id: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 30.0
    max_calls: int = DEFAULT_MAX_CALLS
    tz: int = 8

    def masked(self) -> dict[str, str]:
        return {
            "api_key": f"{self.api_key[:4]}***{self.api_key[-2:]}" if len(self.api_key) > 8 else "***",
            "subscription_id": self.subscription_id,
            "base_url": self.base_url,
        }


def load_settings() -> Settings:
    _load_dotenv()
    key = _first(_KEY_NAMES)
    sub = _first(_SUB_NAMES)
    if not key:
        raise RuntimeError(
            f"缺少 API key：请在 {CREDENTIAL_FILE} 里设置 TJW_API_KEY，或导出同名环境变量。"
        )
    if not sub:
        raise RuntimeError(
            f"缺少订阅 ID：/v2 端点必须带 subscriptionId，请在 {CREDENTIAL_FILE} 里设置 "
            "TJW_SUBSCRIPTION_ID（或 real_sub_id）。"
        )
    return Settings(
        api_key=key,
        subscription_id=sub,
        base_url=os.environ.get("TJW_BASE_URL", DEFAULT_BASE_URL),
        max_calls=int(os.environ.get("TJW_MAX_CALLS", str(DEFAULT_MAX_CALLS))),
    )
