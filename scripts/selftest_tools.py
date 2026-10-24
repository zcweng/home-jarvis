#!/usr/bin/env python3
"""工具层自检：别名解析 → HA 控制 → 状态回读。

这一步不碰语音，只验证"工具契约 + HA 链路"是否正确。
接真实米家设备前，用 HA 的 demo 虚拟灯跑通全部动作是值得的——
将来控灯失灵时，可以立刻分清是"工具层坏了"还是"语音层坏了"。

    .venv/bin/python scripts/selftest_tools.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import Settings  # noqa: E402
from jarvis.ha import HAClient  # noqa: E402
from jarvis.tools import Tools  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    st = Settings.load()
    ha = HAClient(st)
    tools = Tools(ha)

    print(f"HA {st.ha_url} · {ha.ping()}")
    print(f"别名表：{len(tools.devices)} 台设备\n")

    # 不再硬编码设备名：取别名表里第一盏「在线」的灯，demo 虚拟灯 / 真实米家设备都适用
    online = [
        d for d in tools.devices.values()
        if d.type == "light" and (ha.state(d.entity_id) or {}).get("state") not in (None, "unavailable")
    ]
    if not online:
        print("❌ 别名表里没有在线的灯，先完成米家集成 OAuth 或手动指定设备")
        return 1
    dev = online[0]
    target = dev.name
    alias = dev.aliases[0] if dev.aliases else dev.name
    print(f"测试目标：{target}（{dev.entity_id}）\n")

    # 1. 别名表里的实体是否都真实存在
    missing = [d.entity_id for d in tools.devices.values() if ha.state(d.entity_id) is None]
    check("别名表实体存在", not missing, f"缺失 {missing}" if missing else "全部命中")

    # 2. 解析：精确 / 别名 / 房间说法 / 不存在
    check("解析-精确名", tools.resolve(target) is not None, str(tools.resolve(target).entity_id))
    check("解析-别名", (r := tools.resolve(alias)) is not None and r.name == target, f"{alias} → {target}")
    check("解析-不存在", tools.resolve("火星灯") is None, "不存在的设备返回 None")

    # 3. 控制：开 → 亮度 → 相对调整 → 关 → 查询
    print()
    r = tools.control(target, "on")
    st_on = (ha.state(dev.entity_id) or {}).get("state")
    check("开灯", st_on == "on", f"{r} | state={st_on}")

    tools.control(target, "brightness", 30)
    b = (ha.state(dev.entity_id) or {}).get("attributes", {}).get("brightness")
    pct = round(b / 255 * 100) if b is not None else -1
    check("设亮度 30%", abs(pct - 30) <= 2, f"实际 {pct}%")

    r = tools.control(target, "brightness_delta", -10)
    b2 = (ha.state(dev.entity_id) or {}).get("attributes", {}).get("brightness")
    pct2 = round(b2 / 255 * 100) if b2 is not None else -1
    check("相对调暗 -10", abs(pct2 - 20) <= 3, f"{r} | 实际 {pct2}%")

    r = tools.query(target)
    check("查询状态", "亮度" in r or "开" in r, r)

    r = tools.control(target, "off")
    st_off = (ha.state(dev.entity_id) or {}).get("state")
    check("关灯", st_off == "off", f"{r} | state={st_off}")

    # 4. 边界：不存在的设备不能瞎编
    r = tools.control("火星灯", "on")
    check("未知名设备", "没找到" in r, r)

    r = tools.control(target, "飞到天上去")
    check("不支持的动作", "不支持" in r, r)

    # 5. 场景 / 定时
    r = tools.scene("睡觉")
    check("场景-不存在时给清单", "场景" in r, r)

    r = tools.timer(0.02, "自检")
    check("定时器", "提醒" in r, r)

    # 6. 离线设备必须诚实拒控（墙上开关关着的灯就是这个状态）
    offline = [
        d for d in tools.devices.values()
        if (ha.state(d.entity_id) or {}).get("state") == "unavailable"
    ]
    if offline:
        od = offline[0]
        r_ctl = tools.control(od.name, "on")
        r_qry = tools.query(od.name)
        check(f"离线设备拒控：{od.name}", "不在线" in r_ctl, r_ctl)
        check(f"离线设备查询：{od.name}", "离线" in r_qry, r_qry)
    else:
        print("（当前没有离线设备，跳过离线断言）")

    # 7. 播报：发现音箱 / 拒绝未知目标 / 长文本切分（dry-run，不出声）
    dry = Tools(ha, settings=st, dry_run=True)
    eid, name = dry._speaker_notify()
    check("发现可播报音箱", bool(eid) and eid.startswith("notify."), f"{eid.split('.')[-1][:34]} · {name[:16]}")
    r = dry.announce("该吃饭了")
    check("播报 dry-run", "dry-run" in r, r)
    r = dry.announce("该吃饭了", target="火星音箱")
    check("未知播报目标", "没找到" in r, r)
    from jarvis.output import split_for_speech

    parts = split_for_speech("一二三四五六七八九十" * 3, 20)
    check("长文本切分", len(parts) >= 2 and all(len(x) <= 20 for x in parts), f"{len(parts)} 段")
    r = dry.announce("")
    check("空播报文本", "没有要播报" in r, r)

    # 8. 给模型的设备清单非空且可读
    cat = tools.catalog()
    check("设备清单", len(cat.splitlines()) == len(tools.devices), f"{len(cat.splitlines())} 行")

    ha.close()
    ok = all(r[1] for r in results)
    print("-" * 56)
    print("全部通过" if ok else f"失败 {sum(1 for r in results if not r[1])} 项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
