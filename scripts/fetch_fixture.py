"""抓一次真实响应存成 fixture，供离线测试使用。

额度有限：整个脚本只发 1 次请求（除非要素数超限才会切分）。
用法：.venv/bin/python scripts/fetch_fixture.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tianji_power_mcp.client import TianjiClient  # noqa: E402

FIELDS = ["t2m", "rh2m", "ws10m", "ws100m", "wd100m", "tp", "pres", "cldt", "ssrd"]
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample.json"


def main() -> int:
    client = TianjiClient()
    series = client.query("112.55,37.87", FIELDS, hours=24)
    if not series.rows:
        print("没有取到数据，未写 fixture", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "location": "112.55,37.87",
        "fields": series.fields,
        "units": series.units,
        "rows": series.rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": "真实响应样本，仅用于离线测试；不含凭据",
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {OUT}：{len(series.rows)} 行，要素 {series.fields}，调用 {client.calls} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
