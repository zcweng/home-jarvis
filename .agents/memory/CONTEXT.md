# Project Context

Last updated: 2026-10-24T12:38:28.254Z

## Summary

P0 本机语音闭环已在 MacBook Pro M4 上完整跑通并切到真实米家设备：ha_xiaomi_home v0.5.0 安装完成、OAuth 已授权（中国大陆），aliases 收录 10 台真设备，工具层与端到端都用吸顶灯实灯验证通过。本轮新增收线机制（说退下吧 → 先播告别 → 播完才关 FOLLOW_UP 窗口）与离线设备拒控（修掉米家云对 unavailable 实体「返回成功但什么都不做」的第二形态幻觉）。demo 虚拟设备已彻底清除（关键是删 config entry 而非 YAML）。另完成 Xiaomi 智能音箱 Pro OH2P 刷机评估：当喇叭零刷机即可（play_text 实测 200），当耳朵必须刷机且固件须恰好 v1.58.6、上游已归档。

## Key points

- OAuth 已完成：xiaomi_home v0.5.0 状态 loaded（昌文:1263518240 [中国大陆]），internal_url 必须为 http://localhost:8123，回调不能用 127.0.0.1
- config/aliases.yaml 现有 10 台真设备；gen_aliases.py 重写后从 90 条噪音收敛，具备房间识别、噪音过滤、只追加不覆盖、--rebuild/--prune-missing/--include-unavailable/--selftest
- demo 虚拟设备只删 configuration.yaml 无效——HA 会把 YAML 导入成 config entry，必须删配置条目；实体数 359→260
- 收线：try/finally 包住「说告别 → gate.sleep()」，播完告别才关窗；收线词需剥虚词后整句比对，避免「把空调退出节能模式」误判；12 项断言语义测通过
- 离线设备拒控：unavailable 实体的米家调用返回 200 但不执行，现已前置查在线并如实拒绝（控制/查询各 1 断言 + 真模型复测）
- --rebuild 曾会静默删掉离线设备，改为「已收录即保留」并标 [离线]；顺带删掉无代码读取的 pinned 字段
- 自检改为动态选第一盏在线灯，不再硬编码设备名；selftest_all.sh 9 套件全绿，selftest_wake.py 19 项通过
- OH2P 当喇叭零风险：notify play_text 实测 HTTP 200；当耳朵须刷机（16kHz 单声道 S16_LE → ws://host:4399），保留小爱 6 麦远场与原生唤醒词
- 刷机硬门槛：固件必须恰好 v1.58.6，上游 2026-04-04 归档无活跃 fork，server wheel 仅 manylinux_x86_64（arm64 需 Docker+Rosetta）
- 建议顺序：先 announce 工具走 HA notify 当喇叭 → 耳朵走 ESP32-S3 → 确认远场拾音是刚需再考虑刷机

## Open tasks

- 查 Xiaomi 智能音箱 Pro (OH2P) 固件版本（米家 App → 设备 → 关于），只有 1.58.6 才可刷机
- 决定是否实现 announce 工具（走 HA notify 让音箱播报），把操作反馈播到小爱而非电脑喇叭
- 打开墙上开关让 philips 客厅灯上线，验证其控制与状态查询
- 插入有线耳机后重跑 preflight 确认「外置麦克风」，调 VAD_THRESHOLD 与 BARGE_IN_MARGIN_DB
- 接真人语音复查短指令同音字（如「再暗一点」→「再按一点」）识别稳定性
- P0 稳定后迁移 MacMini：clone → uv sync → fetch_models.sh → 改 .env → compose up
- P1（暂缓）：采购约 ¥110 外围件，按 bread-compact-wifi 接线，ESP-IDF ≥6.0.1 编译烧录

<!-- latest-session-title: home-jarvis：米家真设备接入完成，demo 清理，OH2P 刷机评估 -->
