#!/usr/bin/env python3
"""把 HA 长期访问令牌写进 .env，并当场验证。

用法：
    python3 scripts/set_ha_token.py                # 交互式粘贴（推荐，不进 shell 历史）
    python3 scripts/set_ha_token.py eyJ0eXAi...    # 直接传参

行为：
  1. 调 GET {HA_URL}/api/ 验证令牌（期望 200）
  2. 验证通过才写入 .env 的 HA_TOKEN=，并确保 .env 在 .gitignore 里
  3. 验证失败不落盘，避免把错令牌写进去后还要排查半天
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


def die(msg: str) -> None:
    print(f"❌ {msg}")
    raise SystemExit(1)


def read_env() -> dict[str, str]:
    if not ENV.exists():
        die(f"没有 {ENV}，先执行：cp .env.example .env")
    out: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Z_0-9]+)\s*=\s*(.*?)\s*(?:#.*)?$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def verify(base: str, token: str) -> None:
    req = urllib.request.Request(
        base.rstrip("/") + "/api/",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            die("令牌无效（HTTP 401）。确认复制完整、没有多余空格，或重新创建一个")
        die(f"HTTP {e.code}：{e.read()[:200].decode('utf-8', 'ignore')}")
    except urllib.error.URLError as e:
        die(f"连不上 {base}（{e.reason}）。HA 容器起来了吗？")
    try:
        msg = json.loads(body).get("message", "")
    except json.JSONDecodeError:
        msg = body[:80]
    if "API running" not in msg:
        die(f"返回内容不是预期的 HA API 响应：{msg!r}")
    print(f"✅ 令牌有效：{msg}")


def write_token(token: str) -> None:
    text = ENV.read_text(encoding="utf-8")
    if re.search(r"^HA_TOKEN=.*$", text, flags=re.M):
        new = re.sub(r"^HA_TOKEN=.*$", f"HA_TOKEN={token}", text, count=1, flags=re.M)
    else:
        new = text.rstrip() + f"\nHA_TOKEN={token}\n"
    ENV.write_text(new, encoding="utf-8")
    os.chmod(ENV, 0o600)  # 只有自己能读
    print(f"✅ 已写入 {ENV}（权限 600）")

    gi = ROOT / ".gitignore"
    if gi.exists() and re.search(r"^\.env$", gi.read_text(encoding="utf-8"), flags=re.M):
        print("✅ .gitignore 已忽略 .env")
    else:
        print("⚠️  .gitignore 里没有 .env，务必补上，否则令牌会被提交")


def main() -> int:
    env = read_env()
    base = env.get("HA_URL") or "http://127.0.0.1:8123"

    if len(sys.argv) > 1:
        token = sys.argv[1].strip()
    else:
        print(f"HA 地址：{base}")
        print("粘贴长期访问令牌（输入不回显）：", end="", flush=True)
        import getpass

        token = getpass.getpass("").strip()

    if not token:
        die("令牌为空")
    if len(token) < 80:
        print(f"⚠️  令牌只有 {len(token)} 字符，HA 的正常长度约 180+，可能没复制全")

    verify(base, token)
    write_token(token)
    print("\n下一步：python3 scripts/preflight.py  →  开始写 jarvis/audio.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
