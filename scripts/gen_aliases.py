#!/usr/bin/env python3
"""从 Home Assistant 现有实体生成/维护 `config/aliases.yaml`（中文别名 → entity_id）。

米家集成 OAuth 完成后，家里一下子多出两三百个实体：灯、插座、摄像头指示灯、
音箱麦克风静音、鱼缸水泵…… 手工挑能控的那几台太费劲。

这个脚本做四件事：
1. 只保留「家人真的会说出口」的设备（按品类过滤掉指示灯/静音/童锁/水泵这类子功能）
2. 清洗实体名："吸顶灯  灯" → "吸顶灯"，"Xiaomi 智能音箱 Pro  音箱" → "小爱音箱"
3. 从名字里猜房间（客厅/主卧/厨房…），供"卧室的灯"这类说法使用
4. 默认**只追加不覆盖**，已有条目（你手改过的名字、别名）一律不动

用法：
    .venv/bin/python scripts/gen_aliases.py --dry-run           # 先看会生成什么
    .venv/bin/python scripts/gen_aliases.py                     # 追加新设备（自动备份）
    .venv/bin/python scripts/gen_aliases.py --rebuild           # 整体重建（会丢掉手改名）
    .venv/bin/python scripts/gen_aliases.py --prune-missing     # 清掉 HA 里已不存在的实体
    .venv/bin/python scripts/gen_aliases.py --include-unavailable
    .venv/bin/python scripts/gen_aliases.py --selftest          # 只测命名/房间逻辑
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ALIASES = ROOT / "config" / "aliases.yaml"

# 品类 → (中文说明, 冲突时的保留优先级；数字小的优先)
DOMAINS: dict[str, tuple[str, int]] = {
    "cover": ("窗帘", 1),
    "climate": ("空调", 1),
    "vacuum": ("扫地机", 1),
    "lock": ("门锁", 1),
    "switch": ("开关", 2),
    "fan": ("风扇", 2),
    "media_player": ("播放器", 3),
    "light": ("灯", 9),  # 和别的设备重名时灯让路："晾衣架" 归晾衣架，灯叫"晾衣架灯"
}

ROOMS = (
    "儿童房", "衣帽间", "卫生间", "洗手间", "厨房", "餐厅", "客厅", "主卧", "次卧",
    "卧室", "书房", "阳台", "玄关", "走廊", "楼梯", "阁楼", "储藏室", "车库", "露台",
)

# 不是"能控设备"的实体（米家集成会把每个属性都暴露成实体）
NOISE = (
    "传感器", "网关", "子设备", "温度计", "湿度计", "电量", "信号", "固件",
    "指示灯", "麦克风", "静音", "童锁", "提示音", "睡眠模式", "勿扰", "水泵",
    "摄像机", "巡航", "移动侦测", "宽动态", "水印", "微光", "报警", "自定义",
    "物理控制锁", "辅助电加热", "低噪", "推送开关",
)

# friendly_name 里集成加的功能后缀
GENERIC_TAILS = {"灯", "开关", "开关 开关", "灯 灯", "音箱", "空调", "窗帘", "照明", "晾衣架", "电视", "风扇"}

# 太长不好念的产品名 → 口语版本
SHORTEN = {
    "小米米家智能插座WiFi版": "智能插座",
    "Xiaomi 智能音箱 Pro": "小爱音箱",
    "米家智能鱼缸": "鱼缸",
    "Yeelight 皎月LED吸顶灯": "皎月吸顶灯",
    "小米智能摄像机2 云台版": "摄像机",
    "米家微波炉": "微波炉",
}

HEADER = (
    "# 设备别名表：中文口语 → Home Assistant entity_id\n"
    "# 由 scripts/gen_aliases.py 生成，可随意手改；重跑只追加，不会覆盖你的改动（--rebuild 除外）\n"
    "#\n"
    "# 只放「家人真的会说出口」的设备。别名表越长，模型选错的概率越高。\n"
)


# ---------------------------------------------------------------- 命名


def _strip_tail(name: str) -> str:
    parts = name.split("  ") if "  " in name else name.split(" ")
    if len(parts) > 1:
        tail = " ".join(parts[1:]).strip()
        if tail in GENERIC_TAILS or tail == parts[0]:
            return " ".join(parts[0].split())
    return name


def clean_name(friendly: str) -> str:
    """去掉集成加的功能后缀、缩短产品名，收敛到稳定为止。"""
    name = " ".join(friendly.split())
    for _ in range(4):
        before = name
        name = _strip_tail(name)
        for long, short in SHORTEN.items():
            if name.startswith(long):
                name = short + name[len(long) :]
        if name == before:
            break
    return name.strip() or friendly


def guess_room(name: str) -> str:
    for room in ROOMS:
        if room in name:
            return room
    return ""


def is_controlable(attrs: dict) -> bool:
    return attrs.get("device_class") not in ("battery", "signal_strength", "temperature", "humidity")


# ---------------------------------------------------------------- 读写


def load_existing() -> tuple[set[str], set[str]]:
    """返回 (已用别名, 已收录实体)。"""
    if not ALIASES.exists():
        return set(), set()
    names: set[str] = set()
    entities: set[str] = set()
    for line in ALIASES.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith((" ", "#", "-")) and line.rstrip().endswith(":"):
            names.add(line.rstrip()[:-1].strip())
        if "entity:" in line:
            entities.add(line.split("entity:", 1)[1].strip())
    return names, entities


def render_block(alias: str, entity_id: str, domain: str, room: str, spoken: str) -> str:
    lines = [f"{alias}:", f"  entity: {entity_id}", f"  type: {domain}"]
    if room:
        lines.append(f"  room: {room}")
    spoken = " ".join(spoken.split())
    if spoken and spoken != alias:
        lines.append(f"  aliases: [{spoken}]")
    return "\n".join(lines) + "\n"


def collect(ha, domains: set[str], include_unavailable: bool, pinned: set[str] | None = None) -> list[dict]:
    """抓取候选设备列表。"""
    rows: list[dict] = []
    for s in ha.states():
        entity_id = s["entity_id"]
        domain = entity_id.split(".", 1)[0]
        if domain not in domains:
            continue
        attrs = s.get("attributes", {})
        friendly = str(attrs.get("friendly_name") or entity_id.split(".", 1)[1])
        if any(n in friendly for n in NOISE) or not is_controlable(attrs):
            continue
        # 离线的设备默认跳过，但**用户已经显式收录过**的要保留
        # （典型场景：灯的供电在墙上开关，物理关掉时 HA 显示 unavailable）
        if s.get("state") == "unavailable" and not include_unavailable and entity_id not in (pinned or set()):
            continue
        rows.append(
            {
                "domain": domain,
                "entity": entity_id,
                "name": clean_name(friendly),
                "room": guess_room(friendly),
                "spoken": friendly,
                "state": s.get("state"),
            }
        )
    return rows


def resolve_names(rows: list[dict], used_names: set[str]) -> list[dict]:
    """按品类优先级解决重名。"""
    rows = sorted(rows, key=lambda r: (DOMAINS[r["domain"]][1], r["name"]))
    taken = set(used_names)
    for r in rows:
        name = r["name"]
        if name in taken and r["domain"] == "light":
            name = f"{name}灯"
        base = name
        n = 2
        while name in taken:
            name = f"{base}{n}"
            n += 1
        taken.add(name)
        r["alias"] = name
    return rows


def prune_missing() -> int:
    from jarvis.config import Settings
    from jarvis.ha import HAClient

    if not ALIASES.exists():
        print("别名表不存在，不用清理")
        return 0
    st = Settings.load()
    with HAClient(st) as ha:
        alive = {s["entity_id"] for s in ha.states()}

    kept, removed = [], []
    for block in ALIASES.read_text(encoding="utf-8").split("\n\n"):
        ent = ""
        for line in block.splitlines():
            if "entity:" in line:
                ent = line.split("entity:", 1)[1].strip()
                break
        if ent and ent not in alive:
            name = block.splitlines()[0].rstrip(":") if block.strip() else ent
            removed.append(f"{name} → {ent}")
            continue
        kept.append(block)

    if not removed:
        print("别名表里没有失效实体")
        return 0
    backup = ALIASES.with_suffix(".yaml.bak")
    shutil.copy2(ALIASES, backup)
    ALIASES.write_text("\n\n".join(kept).rstrip() + "\n", encoding="utf-8")
    print(f"已清理 {len(removed)} 条失效条目（备份 {backup.name}）：")
    for r in removed:
        print(f"  - {r}")
    return 0


def selftest() -> int:
    cases_name = {
        "吸顶灯  灯": "吸顶灯",
        "吸顶灯2  灯": "吸顶灯2",
        "Yeelight 皎月LED吸顶灯  灯": "皎月吸顶灯",
        "小米米家智能插座WiFi版  开关 开关": "智能插座",
        "Xiaomi 智能音箱 Pro  音箱": "小爱音箱",
        "空调  空调": "空调",
        "晾衣架  晾衣架": "晾衣架",
    }
    cases_room = {
        "客厅吸顶灯": "客厅", "主卧床头灯": "主卧", "儿童房小夜灯": "儿童房",
        "厨房灯": "厨房", "米家智能插座2 蓝牙网关版": "", "小米智能门锁": "",
    }
    bad = 0
    for name, want in cases_name.items():
        got = clean_name(name)
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} 清洗 {name!r} → {got!r}（期望 {want!r}）")
    for name, want in cases_room.items():
        got = guess_room(name)
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'✅' if ok else '❌'} 房间 {name!r} → {got!r}（期望 {want!r}）")

    rows = [
        {"domain": "light", "name": "晾衣架", "entity": "light.x", "room": "", "spoken": "晾衣架"},
        {"domain": "cover", "name": "晾衣架", "entity": "cover.x", "room": "", "spoken": "晾衣架"},
    ]
    out = {r["entity"]: r["alias"] for r in resolve_names(rows, set())}
    ok = out.get("cover.x") == "晾衣架" and out.get("light.x") == "晾衣架灯"
    bad += 0 if ok else 1
    print(f"{'✅' if ok else '❌'} 重名让路 → {out}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    ap.add_argument("--rebuild", action="store_true", help="丢弃现有别名表整体重建（先备份）")
    ap.add_argument("--prune-missing", action="store_true", help="清掉 HA 中已不存在的实体")
    ap.add_argument("--include-unavailable", action="store_true", help="把离线设备也写进去")
    ap.add_argument("--domains", default=",".join(DOMAINS), help="只处理这些 domain")
    ap.add_argument("--selftest", action="store_true", help="只跑命名逻辑自测")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.prune_missing:
        return prune_missing()

    from jarvis.config import Settings
    from jarvis.ha import HAClient

    wanted = {d.strip() for d in args.domains.split(",") if d.strip()} & set(DOMAINS)
    used_names, used_entities = load_existing()
    st = Settings.load()
    with HAClient(st) as ha:
        rows = collect(ha, wanted, args.include_unavailable, pinned=used_entities)

    if args.rebuild:
        used_names, used_entities = set(), set()
    new_rows = [r for r in rows if r["entity"] not in used_entities]
    skipped = len(rows) - len(new_rows)
    resolved = resolve_names(new_rows, used_names)

    if not resolved:
        print("没有需要新增的设备（别名表已覆盖，或 HA 里还没有可控实体）")
        return 0

    blocks = [render_block(r["alias"], r["entity"], r["domain"], r["room"], r["spoken"]) for r in resolved]
    body = "\n" + "\n".join(blocks)

    stats: dict[str, int] = {}
    for r in resolved:
        stats[r["domain"]] = stats.get(r["domain"], 0) + 1
    print(f"新增 {len(resolved)} 台设备" + (f"，跳过已收录 {skipped} 台" if skipped else ""))
    for d, c in sorted(stats.items()):
        print(f"  {DOMAINS[d][0]:<4} {c} 台")
    print()
    for r in resolved:
        print(f"  {r['alias']:<10} {r['entity']:<52} {'[离线]' if r['state'] == 'unavailable' else ''}")

    if args.dry_run:
        print("\n（--dry-run，未写入）")
        return 0

    if ALIASES.exists():
        backup = ALIASES.with_suffix(".yaml.bak")
        shutil.copy2(ALIASES, backup)
        print(f"\n已备份 → {backup.name}")
        if args.rebuild:
            ALIASES.write_text(HEADER + body.lstrip("\n"), encoding="utf-8")
        else:
            ALIASES.write_text(ALIASES.read_text(encoding="utf-8").rstrip() + "\n" + body, encoding="utf-8")
    else:
        ALIASES.write_text(HEADER + body.lstrip("\n"), encoding="utf-8")
    print(f"✅ 已写入 {ALIASES}")
    print("下一步：重启 jarvis（别名启动时加载），然后 `python -m jarvis.app --ptt` 试一句")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
