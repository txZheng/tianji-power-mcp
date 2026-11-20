"""配置与凭据加载。

凭据只从 `.tjweather-rc`（或旧版 `.env`）与环境变量读取；本地状态目录 `.runtime`
锚定在凭据文件所在的「部署根」。

凭据文件定位（0.5.2 起，与宿主设置的 cwd 无关）：

1. 环境变量 ``TJW_CREDENTIAL_FILE`` 指定的文件（最高优先级）；
2. 从本包所在目录**逐级向上**查找（最多 ``WALK_UP_MAX_LEVELS`` 层）——
   源码运行时命中仓库根；被 pip 装进部署目录的 venv
   （``<部署根>/.venv/lib/pythonX.Y/site-packages/...``）时向上命中部署根本身；
3. 当前工作目录（兼容旧行为的最后兜底）；
4. 全部 ``.tjweather-rc`` 候选都没命中时，才按同样顺序找旧版 ``.env``。

``.env`` 永远排在所有 ``.tjweather-rc`` 之后，且读取时遇到不含凭据的文件会继续
往后找，避免路径上的无关 ``.env`` 遮蔽真正的凭据文件。找不到凭据时抛错会列出
全部搜索过的位置与三种解决办法。
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

# 向上查找的层数上限：装进 venv 也只需 5 层
# （包目录 → site-packages → pythonX.Y → lib → .venv → 部署根）
WALK_UP_MAX_LEVELS = 8

_KEY_NAMES = ("TJW_API_KEY", "TIANJI_API_KEY")
_SUB_NAMES = ("TJW_SUBSCRIPTION_ID", "real_sub_id", "TIANJI_SUBSCRIPTION_ID")

# 进程级调用上限：额度有限，默认 20 次/进程，超了直接抛错而不是静默烧额度。
DEFAULT_MAX_CALLS = int(os.environ.get("TJW_MAX_CALLS", "20"))

# 起报发布滞后（小时）：00Z 约 17 时、12Z 约次日 05 时出，即起报后约 9 小时可用。
# 只用于「估算最新起报」，真实取数后会拿响应里的 time_init 校正。
DEFAULT_RUN_LAG_HOURS = float(os.environ.get("TJW_RUN_LAG_HOURS", "9"))
# 取数时多要几小时余量，防止发布延迟导致窗口尾部缺数据；不够会自动补一次。
DEFAULT_PAD_HOURS = int(os.environ.get("TJW_PAD_HOURS", "3"))
# 单次最多取几天（订阅上限一般是 5/10/15/30/45 天，超了会返回 20009）。
DEFAULT_MAX_FETCH_DAYS = int(os.environ.get("TJW_MAX_FETCH_DAYS", "10"))
# 落盘缓存：整段取数结果写盘、跨进程复用，按起报翻页（05:00/17:00）失效。设 TJW_DISK_CACHE=0 关闭。
DEFAULT_DISK_CACHE = os.environ.get("TJW_DISK_CACHE", "1") != "0"


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _walk_up_bases(start: Path, levels: int = WALK_UP_MAX_LEVELS) -> list[Path]:
    """从 start 逐级向上的目录列表（含 start 自身，到文件系统根或层数上限为止）。"""
    bases: list[Path] = []
    current = start
    for _ in range(levels):
        bases.append(current)
        if current.parent == current:
            break
        current = current.parent
    return bases


def _candidate_files(package_dir: Path | None = None) -> list[Path]:
    """按优先级返回凭据文件候选路径。

    环境变量指定时只有它一个候选；否则先列向上各级的 `.tjweather-rc`
    （含 cwd），全部落空后才列旧版 `.env`，避免无关 `.env` 遮蔽。
    """
    override = os.environ.get("TJW_CREDENTIAL_FILE")
    if override:
        return [Path(override).expanduser()]
    bases = _walk_up_bases(package_dir or _package_dir())
    bases.append(Path.cwd())
    files = [base / CREDENTIAL_FILE for base in bases]
    files += [base / name for base in bases for name in LEGACY_CREDENTIAL_FILES]
    return list(dict.fromkeys(files))  # 去重（cwd 可能就在向上路径上），保持顺序


def _credential_search_hint() -> str:
    """凭据缺失时的人话提示：列出搜过的位置 + 三条出路。"""
    bases = _walk_up_bases(_package_dir()) + [Path.cwd()]
    lines = "\n".join(f"  - {base / CREDENTIAL_FILE}" for base in bases)
    return (
        f"已按顺序查找（含各目录下的旧版 {LEGACY_CREDENTIAL_FILES[0]}），均未取得凭据：\n{lines}\n"
        "解决办法（任选其一）：\n"
        "  1. 在上述任一目录放置 .tjweather-rc，写入 TJW_API_KEY 与 TJW_SUBSCRIPTION_ID；\n"
        "  2. 设置 TJW_CREDENTIAL_FILE=<凭据文件绝对路径>；\n"
        "  3. 直接为宿主进程导出 TJW_API_KEY / TJW_SUBSCRIPTION_ID 环境变量。"
    )


def _load_dotenv() -> None:
    """把凭据文件读进 os.environ（不覆盖已存在的环境变量）。

    找到含完整凭据（key + 订阅 ID）的文件就停；中途遇到不含凭据的
    文件（如无关 .env）会继续找后面的候选。
    """
    for candidate in _candidate_files():
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        if _first(_KEY_NAMES) and _first(_SUB_NAMES):
            return


def _first(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _discover_runtime_root(package_dir: Path | None = None) -> Path:
    """本地状态（.runtime）的锚点：凭据文件所在目录。

    装进 venv 时 = 部署根（凭据文件旁边，venv 重建不丢状态）；
    源码运行时 = 仓库根（与旧行为一致）；完全没有凭据文件时退回 PROJECT_ROOT。
    """
    for candidate in _candidate_files(package_dir):
        if candidate.is_file():
            return candidate.parent
    return PROJECT_ROOT


# 状态目录锚点：import 时按上述规则定位一次
RUNTIME_ROOT = _discover_runtime_root()


@dataclass(frozen=True)
class Settings:
    api_key: str
    subscription_id: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 30.0
    max_calls: int = DEFAULT_MAX_CALLS
    tz: int = 8
    run_lag_hours: float = DEFAULT_RUN_LAG_HOURS
    pad_hours: int = DEFAULT_PAD_HOURS
    max_fetch_days: int = DEFAULT_MAX_FETCH_DAYS
    disk_cache: bool = DEFAULT_DISK_CACHE

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
    if not key or not sub:
        what = "API key（TJW_API_KEY）" if not key else "订阅 ID（TJW_SUBSCRIPTION_ID / real_sub_id）"
        raise RuntimeError(f"缺少{what}。{_credential_search_hint()}")
    return Settings(
        api_key=key,
        subscription_id=sub,
        base_url=os.environ.get("TJW_BASE_URL", DEFAULT_BASE_URL),
        max_calls=int(os.environ.get("TJW_MAX_CALLS", str(DEFAULT_MAX_CALLS))),
        run_lag_hours=float(os.environ.get("TJW_RUN_LAG_HOURS", str(DEFAULT_RUN_LAG_HOURS))),
        pad_hours=int(os.environ.get("TJW_PAD_HOURS", str(DEFAULT_PAD_HOURS))),
        max_fetch_days=int(os.environ.get("TJW_MAX_FETCH_DAYS", str(DEFAULT_MAX_FETCH_DAYS))),
        disk_cache=os.environ.get("TJW_DISK_CACHE", "1") != "0",
    )
