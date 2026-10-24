# Xiaomi 智能音箱 Pro（OH2P）刷机评估：能不能当耳朵和喇叭

> 撰写：2026-10-24 · 结论先行，风险写全，不劝你冲动刷机。

---

## 0. 结论

| 方案 | 能当喇叭 | 能当耳朵 | 风险 | 建议 |
|---|---|---|---|---|
| **A. 不刷机，用米家 `播放文本`** | ✅ 今天就能用 | ❌ | 无 | **先做这个** |
| **B. 刷 Open-XiaoAI** | ✅ | ✅ | 高（见 §5） | 想清楚再动 |
| **C. ESP32-S3 端点**（板子已有，¥110 件） | ✅ | ✅（含 AEC 全双工） | 低 | 主线 |

一句话：**喇叭现在就能借来用，耳朵要付"被停维护固件绑架"的代价。**
OH2P 唯一不可替代的优势是**环形麦阵 + 硬件 AEC 的远场拾音**，如果你家客厅远场识别是刚需，那它值得刷；否则别碰。

---

## 1. 实测：不刷机也能让它说话

米家集成 0.5.0 把这台音箱的属性暴露成了 10 个实体，其中三个对我们关键：

| 实体 | 作用 |
|---|---|
| `notify.xiaomi_cn_100000006_oh2p_play_text_a_7_3` | **播放文本**（让它念任意文本） |
| `button.xiaomi_cn_100000006_oh2p_wake_up_a_7_1` | 唤醒 |
| `notify.xiaomi_cn_100000006_oh2p_execute_text_directive_a_7_4` | 执行文本指令（等于对它"说"一句话） |

> 实体名中间那段数字是设备在米家云里的 ID，各家不同；用
> `python scripts/gen_aliases.py --dry-run` 或 HA「开发者工具 → 状态」搜 `play_text` 看你自己的。

2026-10-24 实测调用成功：

```bash
curl -X POST "$HA_URL/api/services/notify/send_message" \
  -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id":"notify.xiaomi_cn_100000006_oh2p_play_text_a_7_3",
       "message":"你好，我是贾维斯，正在测试通过米家接口说话。"}'
# → HTTP 200，实体状态更新为时间戳
```

**限制**（都很实在）：
- 走**小米云 TTS**，音色固定、不可选，延迟约 1 秒
- 单次文本长度有限（长段落会被截断，建议按句切分）
- 勿扰时段（你家设的是 21:35–06:35）可能直接被静音
- **拿不到麦克风流**——耳朵这件事，这条路完全不通

---

## 2. 刷机能得到什么

| 能力 | 说明 |
|---|---|
| 麦克风原始音频 | 16kHz / 单声道 / S16_LE 实时流，推到局域网 `ws://<server>:4399` |
| 喇叭原始音频 | 服务端推 PCM 回去就能播（同样 16k S16_LE） |
| 原生事件流 | 小爱自己的 ASR 结果（`SpeechRecognizer.RecognizeResult`）、播放状态、音量 |
| 执行 shell | 服务端可下发脚本（改音量、点灯环、重启等） |
| 唤醒词 | 可自定义（示例里是"豆包豆包 / 你好小智 / hi siri"），也可继续用小爱原生唤醒 |

**最有价值的一条**：它保留了小爱**已经训练好的唤醒词 + 远场 ASR**。也就是说你可以把"唤醒"交给音箱（6 麦环阵远超 DIY 双麦），只管把识别出来的文本接到我们自己的 LLM。

---

## 3. 刷机能失去什么

- 原厂小爱语音助手（问天气/点歌/控制别的设备）**基本作废**，它变成一块"你家的麦克风+喇叭"
- 米家 App 里的部分功能/OTA 升级失效
- **保修**（拆机风险、官方明确写"后果自负"）
- 系统一旦升级就脱离补丁，需**重新刷机**

---

## 4. 硬性前置条件（不满足就别开始）

1. **型号必须是 OH2P** —— 你的 HA 实体名形如 `xiaomi_cn_<ID>_oh2p_*`（`<ID>` 是米家云设备 ID，各家不同），
   到 HA「开发者工具 → 状态」搜 `oh2p` 确认；实体名里带 `oh2p` 就说明型号对上了 ✅
2. **固件必须恰好是 v1.58.6** —— 官方补丁只支持这一个版本，**跨版本刷会变砖**
   - 查版本：米家 App → 该音箱 → 右上角设置 → 关于/版本
   - 高于 1.58.6：官方明确写"可能存在变化，导致刷机失败，设备变砖" → 要么放弃，要么自己按 `packages/client-patch` 流程用小米账号抓 OTA 做补丁
   - 低于 1.58.6：可先升级到 1.58.6
3. **Type-C 数据线**（能传数据，不能是纯充电线），OH2P **免拆机**
4. 电脑：Windows 用 Amlogic Flash Tool 6.0.0；**macOS 有专用工具**（`packages/flash-tool`，需 `brew install libusb-compat`）

---

## 5. 风险清单（决定要不要刷之前必须读完）

1. **上游已停止维护**：`idootop/open-xiaoai` 于 **2026-04-04 归档**（README 明确"已停止维护，不再提供更新与支持"）
2. **没有活跃 fork**：查了最近 8 个 fork，全部停在同一个归档时间点，无人在维护
3. **变砖不可逆？** 不完全——仓库提供同版本**原版固件**可刷回，但需要设备仍能被 Amlogic 工具识别；一旦分区写坏就救不回来
4. **服务端生态是 x86_64 Linux**：示例服务端依赖的 Python 包 `open-xiaoai-server`（PyPI 0.1.0）**只有 manylinux x86_64 wheel**，macOS arm64 装不上 → 要么 Docker `linux/amd64` + Rosetta，要么自己实现 WebSocket 协议
5. 音频是 **16kHz**：说话够用，放音乐音质会明显变差

---

## 6. 刷机步骤（照做，别跳步）

> 来源：仓库 `docs/flash.md` + `packages/client-rust/README.md`

```bash
# ① 准备固件：下载 OH2P v1.58.6 的 patched 版本，重命名为 root_patched.squashfs
#    https://github.com/idootop/open-xiaoai/releases/tag/OH2P_1.58.6

# ② macOS 刷机工具
git clone https://github.com/idootop/open-xiaoai.git
cd open-xiaoai/packages/flash-tool
brew install libusb-compat
chmod +x ./flash

# ③ 拔电源重插 → 立刻执行（要有耐心，多试几次直到识别到固件版本号）
./flash connect

# ④ 让 uboot 停在启动菜单，从 boot0 启动
./flash delay 15
./flash switch boot0

# ⑤ 刷补丁固件到 system0
./flash system system0 root_patched.squashfs

# ⑥ 断电重启，SSH 上音箱（默认密码 open-xiaoai）
ssh root@<音箱IP>
```

刷好之后装客户端：

```bash
mkdir -p /data/open-xiaoai
# 指向你的"大脑"地址（现在就是 MacBook / 将来 MacMini 的局域网 IP）
echo 'ws://192.168.x.x:4399' > /data/open-xiaoai/server.txt
curl -sSfL https://gitee.com/idootop/artifacts/releases/download/open-xiaoai-client/init.sh | sh

# 开机自启
curl -L -o /data/init.sh https://gitee.com/idootop/artifacts/releases/download/open-xiaoai-client/boot.sh
reboot
```

> ⚠️ `echo 'ws://...'` 里的地址要填**跑服务端那台机器**的内网 IP，不是音箱自己的。

---

## 7. 刷完之后怎么接进我们现在这套

客户端是"哑终端"：它只做三件事——推麦克风 PCM、推事件、收你要播的 PCM。真正的耳朵是**你的服务端**。

三种接法，从省事到可控：

| 接法 | 做法 | 工作量 |
|---|---|---|
| **7.1 换掉小智地址（最快验证）** | 用 `examples/xiaozhi` 的服务端，把 `WEBSOCKET_URL` 指向我们自己的服务，服务端实现小智协议 | 中 |
| **7.2 用 `open_xiaoai_server` 写桥接** | Python 里 `register_fn("on_input_data", cb)` 拿 PCM、`on_output_data(bytes)` 回推 PCM；回调里调我们现成的 `jarvis.handle()` | **推荐**，复用现有管线 |
| **7.3 只用音箱的 ASR 文本** | 监听 `SpeechRecognizer.RecognizeResult` 事件拿文本 → 直接喂 `handle_text()`，跳过我们自己的 ASR/VAD/KWS | 最省，但失去打断与自控唤醒 |

7.2 的关键代码骨架（接我们现有 `jarvis.app`）：

```python
import open_xiaoai_server, numpy as np
from jarvis.app import Jarvis
from jarvis.audio import TARGET_RATE

app = Jarvis()          # ASR / LLM / 工具 / TTS 全都现成

def on_input_data(data: bytes) -> None:
    pcm = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0   # 16k mono
    for utt in app.segmenter.accept(pcm):        # 复用我们的 VAD 断句
        app.handle(utt)                          # 思考 + 播报

def speak_to_speaker(pcm16: np.ndarray) -> None:
    open_xiaoai_server.on_output_data((pcm16 * 32767).astype("<i2").tobytes())

open_xiaoai_server.register_fn("on_input_data", on_input_data)
open_xiaoai_server.start_server()                # 监听 :4399
```

> 注意：`open-xiaoai-server` 目前只有 x86_64 Linux wheel。macOS arm64 上要么用 Docker（`--platform linux/amd64` + Rosetta），要么自己实现这个 WebSocket 协议（消息格式在 `packages/client-rust` 里，音频 16k S16_LE 双向 + JSON 事件）。

---

## 8. 我的建议（分三步，别一次跳到底）

**第一步（已完成 ✅）**：小爱已经能当"喇叭"用了
- `jarvis/tools.py` 新增 `announce` 工具 → HA `notify.send_message` → 音箱念文本
- 实测："让音箱跟家里人说一句：晚饭做好了，快来吃。" → 模型正确调用 announce，并主动提示"它现在在勿扰状态，可能听不见，要不要我先关掉勿扰"
- 自动发现带 `play_text` 的音箱（优先别名表里的 `小爱音箱`）；长文本按句切分；勿扰开启会提示
- 新增 `小爱勿扰` 别名（实测可开关：on → off → on）
- 已把**默认语音输出**切到音箱：`TTS_TARGET=speaker`（`jarvis/output.py` 路由，音箱失败自动回落电脑喇叭）
- 局限仍在：走小米云 TTS、约 1 秒延迟、勿扰时段静音、**拿不到麦克风**、云端音频无法打断（故音箱模式禁用 `--barge-in`）

**第二步（两周内）**：耳朵走 ESP32-S3 端点（板子已有，¥110 外围件）
- 全双工 + 可打断，不动音箱固件，随时能迁 MacMini

**第三步（可选，想清楚再动）**：如果远场拾音确实是痛点，再刷 OH2P
- 前提：版本正好 1.58.6 且你接受"这台音箱以后不再是小爱"

---

## 9. 附：先去查一下固件版本

米家 App → 设备 → Xiaomi 智能音箱 Pro → 右上角 ⚙️ → 关于/版本。

- `1.58.6` → 可以直接刷
- 更高 → 官方无补丁，只能自建（风险更高）→ **建议放弃刷机**
- 更低 → 先升级到 1.58.6 再刷

---

### 来源
- [OFFICIAL] `docs/flash.md`（刷机流程、型号与固件版本限制）：https://github.com/idootop/open-xiaoai/blob/main/docs/flash.md
- [OFFICIAL] `packages/client-patch/README.md`（只支持 OH2P v1.58.6 / LX06 v1.94.13）：https://github.com/idootop/open-xiaoai/blob/main/packages/client-patch/README.md
- [OFFICIAL] `packages/client-rust/README.md`（客户端安装、server.txt、开机自启）：https://github.com/idootop/open-xiaoai/blob/main/packages/client-rust/README.md
- [OFFICIAL] `examples/xiaozhi/README.md`（Python 服务端示例、连续对话/打断/自定义唤醒词）：https://github.com/idootop/open-xiaoai/blob/main/examples/xiaozhi/README.md
- [OFFICIAL] 音频参数：`packages/client-rust/src/services/audio/config.rs`（channels=1, sample_rate=16000）
- [VERIFIED] PyPI `open-xiaoai-server` 0.1.0 仅提供 `manylinux_2_34_x86_64` wheel
- [VERIFIED] 仓库归档时间 2026-04-04；最近 8 个 fork 均未继续维护
