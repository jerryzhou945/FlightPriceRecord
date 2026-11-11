"""改写 .env 里的个别键（界面里填微信推送口令时用），其他行原样保留。

密钥只落在 .env（不进仓库）和当前进程的环境变量里，不写数据库、不进日志、不通过接口返回。
"""

from __future__ import annotations

import os
from pathlib import Path


def update_env_file(path: Path, values: dict[str, str | None]) -> None:
    """把 values 写进 .env 并同步到 os.environ；值为 None 或空串表示删除这个键。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#")
            else ""
        )
        if key in pending:
            value = pending.pop(key)
            if value:
                out.append(f"{key}={value}")
            else:
                out.append(f"{key}=")  # 保留这一行，方便用户看到有这个配置项
            continue
        out.append(line)
    out += [f"{key}={value}" for key, value in pending.items() if value]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    for key, value in values.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
