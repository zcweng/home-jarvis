# S12 音箱 AEC 数据流方案（SpeexDSP + ch7 电气参考）

> 目标：把小米 AI 音箱 **S12（DKSND-S12-ECHO-AB-20170210 / ROM 1.74.18）**做成 home-jarvis 的
> 「远场麦 + 好喇叭」网络音频端点，**并且能真的边播边说（全双工打断）**。
>
> 上游背景与硬件侦察见 `docs/小米AI音箱-MDZ-25-DA-接入清单.md` §16。本文只解决一件事：
> **回声怎么消、数据怎么流、代码怎么落。**
>
> 撰写日期：2026-11-02　基线：S12 / 1.74.18 / 已开 root + dropbear

---

## 0. 结论先行

| 问题 | 结论 |
| --- | --- |
| AEC 能做吗 | ✅ 能做，**不需要小米的 Dirac/`libxaudio_engine.so`** |
| 参考信号哪来 | ✅ **`ch7` 是播放 PCM 的数字环回拷贝**（不是模拟回采），与 6 路麦克风在**同一个 ALSA buffer**里 |
| 用什么算法 | 板载 `libspeexdsp.so.1.5.0` 的 **MC-MDF AEC**（6 mic × 1 ref） |
| 算力够吗 | ✅ 4×Cortex-A53 + NEON，240MB 内存 / 141MB 可用；估算 AEC 占单核 20~40%，**但必须实测** |
| 音量怎么控 | ❗ **四个混音器控件全部无效**，音量只能软件做（§4.5.1） |
| 最大风险 | ❗ **只能写右声道**——写左声道完全没声音（§4.5.3） |
| 落地方式 | 设备侧交叉编译一个 **静态 aarch64 守护进程**（设备无 gcc / 无 python） |

**一句话原理**：`ch7` 让我们拿到了「此刻正在送进 DAC 的样本」的**逐样本数字真值**（幅度误差 −0.02 dB，静音段恰好为 0），
所以 AEC 从「盲估计回声路径」退化成「估计一小段残差延迟 + 房间混响尾巴」——
这是 S12 相比「Mac 上拿扬声器声音回采」最大的结构性优势，也是选它做主端点的核心理由。

> 注：`ch7` 在 DAC **之前**，所以参考里**不含**功放/扬声器的非线性失真。
> 这部分 Speex MDF（线性滤波器）本来也消不掉，影响次要（§4.5.2）。

---

## 1. 两个必须先纠正的前提

### 1.1 §16.9 里的「-5ms ~ +64ms 通道时延」**不能**当波束成形延迟用

上游文档 §16.9 表 3 记的是「包络互相关滞后」。这个量级在物理上**不可能是阵列几何时延**：

```
6 麦阵列口径约 5~8 cm
最大 TDOA = 0.08 m / 343 m·s⁻¹ ≈ 0.23 ms ≈ 11 samples @48k
```

而实测值是 **-5ms ~ +64ms**，比几何上限大 1~2 个数量级。这些滞后是
**房间多径 + 混响 + 箱体共振**在慢包络上的投影，不是直达路径。

因此：

- ✅ 这个结论**仍然成立**：ch0-5 确实拾取到了真实回声（相关系数 0.35~0.37），AEC 后有效果可期待
- ✅ 这个结论**仍然成立**：ch7 与播放源相关 1.0000，是干净参考
- ❌ **不能**把 -5/+64ms 当作 `delay_and_sum` 的补偿量——那样会把直达声相位打散，波束成形变成随机加权

**V2 做波束成形时，TDOA 必须用 GCC-PHAT 在宽频上现算**（见 §4.9）。

### 1.2 SpeexDSP 是 16-bit 定点/整型接口，不是浮点

`speex_echo_cancellation()` / `speex_preprocess_run()` 的入参是 `spx_int16_t*`。
而采集是 **S32_LE**、麦克风电平只有 **-44 dBFS**。
若直接 `>>16` 转 S16，有效位只剩约 **10 bit**，AEC 收敛会明显变差。

→ 必须 **先在 float 域做高阻滤波 + 增益归一化，再转 S16**（§4.4）。这一条决定 AEC 效果好坏，
比换算法重要得多。

---

## 2. 硬件与软件基线（2026-11-02 实测）

### 2.1 计算平台

| 项 | 实测 | 影响 |
| --- | --- | --- |
| SoC | Amlogic GXL，`CPU part 0xd03` = **Cortex-A53 ×4** | 可用 4 核，AEC 单独占一核 |
| 指令集 | `fp asimd`（**有 NEON**） | 重采样可走 NEON 路径 |
| 内核 | `Linux 3.14.29 aarch64` | 老内核，静态二进制最稳 |
| 内存 | `MemTotal 240932 kB` / `MemAvailable 140908 kB` | 守护进程预算 < 20MB，宽裕 |
| C 库 | `/lib/libc-2.19.so`，`/lib/ld-linux-aarch64.so.1` | **glibc 2.19**，新工具链产物会 `GLIBC_2.28 not found` |
| 编译器 | ❌ 无 `gcc` / `cc` / `clang` | **必须交叉编译** |
| Python | ❌ 无 | 同上 |
| 磁盘 | `/` 剩余 46.9M，`/data` 剩余 13.4M | 2MB 二进制完全够 |

### 2.2 音频库

| 文件 | 说明 |
| --- | --- |
| `/usr/lib/libspeexdsp.so.1.5.0` | 54KB，导出 `speex_echo_state_init_mc` / `speex_preprocess_*` / `speex_resampler_*` **全套** |
| `/usr/lib/libasound.so.2.0.0` | ALSA lib |
| `/usr/lib/alsa-lib/libasound_module_rate_speexrate*.so` | ALSA 侧重采样插件（**本方案不用**，见 §4.6） |
| 头文件 | ❌ 无 `/usr/include/speex/`，需自备 |
| dev 符号链接 | ❌ 无 `libspeexdsp.so`，`-lspeexdsp` 会失败 |

### 2.3 ALSA 混音器（`amixer controls`，pns 停止后）

```
numid=1  DAC Digital Playback Volume     实测无效（251→100 幅度不变，见 §4.5.1）
numid=2  DAC Extra Digital Gain          实测无效（0→18dB 幅度不变）
numid=6  Ext Spk Switch                  实测无效（保留 on 即可）
numid=3  Audio i2s mute                  实测无效（保留 off）
numid=7/8/9/10  Lineout left/right N/P   实测无效（四路都接回基线值）
numid=4  Output Swap                     实测无效（L/R、R/L、L/L 输出完全相同）
numid=11 mysoftvol                       实测无效（60→255→0 幅度不变）
numid=12 notifyvol                       实测无效（pns 专用，接管后不用）
```

⚠️ **整块混音器对播放通路是「只读装饰」**——换句话说，**这个机型没有任何硬件音量控制手段**，
音量必须 100% 在软件里乘算（§4.5.4）。这跟常见的 Amlogic 板子不一样，别按经验办事。

⚠️ **没有麦克风增益 / AGC 控件**。麦克风增益是固定的，只能靠软件补。这也是 §4.4 必须做归一化的原因。

### 2.4 采集/播放格式

厂商 `mipns-xiaomi` 运行时的真实参数（从 `/proc/asound/card0/pcm0{c,p}/sub0/` 读到，只读不改）：

```text
pcmC0D0c  state=RUNNING  access=MMAP_INTERLEAVED  format=S32_LE  ch=8  rate=48000
          period_size=512   buffer_size=8192        ← 10.7ms / 171ms

pcmC0D0p  state=RUNNING  access=MMAP_INTERLEAVED  format=S32_LE  ch=2  rate=48000
          period_size=8192  buffer_size=24576       ← 171ms / 512ms
```

三个必须注意的点：

1. **播放也可以是 `S32_LE`**（厂商就是用它，不是 S16）。本方案下行统一用 S16 是为了省带宽，
   但播放端必须**同时接受 S16 与 S32**——`HW_PARAMS` 里先试 S16，失败就退 S32 并左移 16 位。
2. **厂商的 buffer 极大**（播放 512ms）。这是云端 TTS 流式播放的典型配置，
   代价是首字延迟不可控。本方案用 **10ms period / 30~50ms buffer**，
   把延迟换回来——因为我们的音频是 Mac 侧流式生成，不需要那么深的缓冲。
3. **采集 period 是 512 而不是 480**。厂商按 512 对齐（不是 10ms 整数倍），
   而我们要 `period_size` 与 AEC `frame_size=160@16k` 严格 3:1 对齐，
   所以**改为 480（=160×3）**。ALSA 会接受，因为只要求 period 是 2 的幂或能被帧数整除，
   `480 = 2^5 × 15` 对 `S32_LE × 8ch` 是合法帧长。

**采集与播放共用同一个音频 PLL** → 两端采样率**不存在钟漂**，可以放心用固定长度缓冲区，不需要
adaptive resampling / ASRC。这一条省掉一个小型子系统。

---

## 3. 信号流总图

```
┌────────────────────────────── S12 设备侧 ──────────────────────────────┐
│                                                                        │
│  ┌──────────┐   8ch/S32/48k   ┌──────────────────────────────────────┐ │
│  │  ADC     │───────────────► │ ① 解交织                              │ │
│  │  6 mic   │                 │   ch0-5 → near[6]                    │ │
│  │  + ch7   │                 │   ch7    → ref                       │ │
│  └──────────┘                 │   ch6    → 丢弃                      │ │
│                               └───────────────┬──────────────────────┘ │
│                                               │                        │
│                    ┌──────────────────────────┴───────────────┐        │
│                    ▼ near[6]                                 ▼ ref    │
│        ┌───────────────────────┐              ┌────────────────────┐  │
│        │ ② DC 阻断 (80Hz HPF)  │              │ ②' DC 阻断         │  │
│        │    float32 域        │              │    float32 域      │  │
│        └──────────┬────────────┘              └─────────┬──────────┘  │
│                   ▼                                     ▼              │
│        ┌───────────────────────┐              ┌────────────────────┐  │
│        │ ③ 归一化 + 固定增益   │              │ ③' 参考归一化      │  │
│        │    → S16 (~-12dBFS)   │              │    → S16           │  │
│        └──────────┬────────────┘              └─────────┬──────────┘  │
│                   ▼                                     ▼              │
│        ┌───────────────────────┐              ┌────────────────────┐  │
│        │ ④ 重采样 48k → 16k    │              │ ④' 重采样 48k→16k  │  │
│        │   (同一配置, 3:1)     │              │    (同一配置)      │  │
│        └──────────┬────────────┘              └─────────┬──────────┘  │
│                   └──────────────┬─────────────────────┘              │
│                                  ▼                                     │
│                   ┌───────────────────────────────┐                    │
│                   │ ⑤ SpeexDSP MC-AEC             │                    │
│                   │   frame=160 (10ms) @16k       │                    │
│                   │   filter=3200 (200ms)         │                    │
│                   │   nb_mic=6, nb_play=1         │                    │
│                   └───────────────┬───────────────┘                    │
│                                   ▼  echo-cancelled ×6                 │
│                   ┌───────────────────────────────┐                    │
│                   │ ⑥ 通道选择 / 波束成形         │                    │
│                   │   V1: 按 post-AEC SNR 选 1    │                    │
│                   │   V2: GCC-PHAT + delay&sum    │                    │
│                   └───────────────┬───────────────┘                    │
│                                   ▼ mono S16 @16k                      │
│                   ┌───────────────────────────────┐                    │
│                   │ ⑦ speex_preprocess_run        │                    │
│                   │   NS=on, AGC=off, VAD=off     │                    │
│                   └───────────────┬───────────────┘                    │
│                                   ▼                                    │
│                   ┌───────────────────────────────┐                    │
│                   │ ⑧ UDP/TCP 上行  raw PCM       │───► Mac            │
│                   └───────────────────────────────┘                    │
│                                                                        │
│   ◄──── 下行 (framed: PCM / FLUSH / VOLUME)                            │
│                   ┌───────────────────────────────┐                    │
│                   │ ⑨ 抖动缓冲 + 软件音量         │                    │
│                   └───────────────┬───────────────┘                    │
│                                   ▼                                    │
│                   ┌───────────────────────────────┐                    │
│                   │ ⑩ 重采样 16k → 48k            │                    │
│                   └───────────────┬───────────────┘                    │
│                                   ▼ 2ch S16 @48k                       │
│                   ┌───────────────────────────────┐                    │
│                   │ ⑪ ALSA 播放 pcmC0D0p          │                    │
│                   │   ⚠️ 左右声道必须写相同样本    │                    │
│                   │   （硬件只取 index 1）        │                    │
│                   └───────────────┬───────────────┘                    │
│                                   │ 数字环回（DAC 之前，实测）          │
│                                   ├──────────────┐                     │
│                                   ▼              │                     │
│                              ┌─────────┐         │                     │
│                              │  DAC    │──► 功放 ──► 喇叭              │
│                              └─────────┘         │                     │
│                                                  ▼                     │
│                                           回到 ① 的 ch7                │
└────────────────────────────────────────────────────────────────────────┘
```

**闭环点**：⑪ 写入的 PCM → ch7 → ① 的 ref。这是整份方案的地基。

它意味着「AEC 的参考信号不存在同步问题」——因为 ref 和 mic 是从**同一帧 ADC 数据**里解出来的。
实测（§4.5.2）ch7 与写入的 PCM **逐样本相同**、静音段恰好为 0，
所以这不只是「近似无损的模拟回采」，而是**数字拷贝**：参考信号在数学上是精确的。

---

## 4. 上行：采集 → AEC → 16k 单声道

### 4.1 ① ALSA 采集参数

| 参数 | 取值 | 理由 |
| --- | --- | --- |
| PCM | `hw:0,0` | 不用 `plughw`/`default`，避免插件层引入未知缓冲 |
| 格式 | `S32_LE` | 硬件原生，转换由我们在 DSP 里做 |
| 声道 | `8` | 硬件原生 |
| 采样率 | `48000` | 硬件原生 |
| `period_size` | **480 帧（10ms）** | 与 AEC 帧率对齐；`8×4×480 = 15360 B/period` |
| `buffer_size` | **2400 帧（50ms，5 个 period）** | 太小会 overrun，太大会增加延迟 |
| 访问方式 | `MMAP_INTERLEAVED`（优选）/ `RW_INTERLEAVED`（回退） | 少一次拷贝 |
| 启动 | `snd_pcm_start` 后循环 `snd_pcm_mmap_begin/commit` | — |

> 设备 `pcmC0D0c` 必须**先释放 pns**：`/etc/init.d/pns stop`（`§16.9` 已验证两者可同时打开）。

### 4.2 ① 通道解交织

```
frame[8] (S32) → near[m] = frame[m],          m = 0..5
                 ref     = frame[7]
                 frame[6]  → 丢弃（恒为 0，未接）
```

`s32 → float`：`f = (float)s32 / 2147483648.0f`。

**不要假设通道顺序永远不变**。首次启动跑一次自检（§11.1），确认 ch6 静音、ch7 与写入的 PCM 一致。

> ⚠️ `ref`（ch7）**不要**做 DC 阻断。它是数字环回，直流分量恰好为 0；
> 加高通只会引入额外的相位旋转，破坏「参考 = 写入样本」这个精确关系。
> §4.3 的滤波器只作用于 ch0-5 这 6 路。

### 4.3 ② DC 阻断（仅 ch0-5）

实测每路麦克风有 **-0.052 ~ -0.059 FS** 的直流偏置（相当于 -110M LSB）。
不做处理会吃掉 6% 动态范围，并让 AEC 的自适应滤波器长时间朝错误方向走。

**不是简单减均值**，用二阶巴特沃斯高通：

```
fc  = 80 Hz          // 男声基频最低 ~85Hz，80Hz 不伤语音
fs  = 48000
Q   = 0.7071         // Butterworth
```

用 RBJ biquad 系数（6 路各一份 state）。**ch7 不过这个滤波器**（见 §4.2）。

> 为什么不用一阶：一阶在 80Hz 只有 -3dB/oct，对 -0.052FS 这种**常量**偏置虽然够用，
> 但对低频漂移（温度/电源纹波）抑制不足，会导致 AEC 的残差一直在动。二阶成本可忽略。

同时做**削顶保护**：`if (|x| > 1.0) clip++`，并在 HTTP `/stats` 里暴露。

### 4.4 ③ 电平归一化（决定 AEC 成败的一步）

实测麦克风电平 **-44 dBFS RMS**，而 Speex 期望的是接近满量程的 S16。

**做法**：固定增益，**不做 AGC**。

| 通道 | 目标 | 增益 |
| --- | --- | --- |
| ch0-5 near | 正常说话 RMS ≈ **-18 dBFS** | +26 dB（×20）*标定值，见下* |
| ch7 ref | **保持原样，不要放大** | **×1.0** |

> ⚠️ 与 mic 增益不同，**ref 必须保持单位增益**。原因：
> ch7 与写入的 PCM 是**逐样本相同**的数字拷贝（§4.5.2），
> 而 PCM 的幅度由我们的软件音量决定。若给 ref 额外乘固定倍数，
> 一旦软件音量调到超过 1/gain_ref，参考就会削顶——而源信号本身并没削顶。
> **保持 ×1.0 时，ref 削顶当且仅当 PCM 削顶**，这个不变量最安全。

标定方法（一次性，写进配置常量）：

1. 设备静音，`arecord` 录 3 秒，统计 ch0-5 各自 RMS → 取中位数 `R_mic`
2. `gain_mic = 0.125 / R_mic`（目标 -18 dBFS）
3. `gain_ref = 1.0`（固定，不标定）
4. 把 `gain_mic` 固化进 `endpoint.conf`，**不再自动调整**

（2026-11-02 重标定：噪声底 ≈ −84 dBFS，正常说话峰值 ≈ −53 dBFS →
`gain_mic ≈ 56`（+35 dB，目标 −18 dBFS）。旧值 265 是把噪声底误当成语音，
会把底噪抬到 ≈ −35 dBFS 并大量削顶，**不要再用**。更早文档里的 −44 dBFS /
`gain≈26.6` 对应更近/更大声的场景，当前摆放以 56 为准。）

**为什么 mic 和 ref 的增益可以不一样**：Speex 的 MDF 自适应滤波器会自己学这个比例，
不一致只是让滤波器系数绝对值变大，不影响收敛（float 域 24bit 尾数足够）。

### 4.5 ⚠️ 音量：**四个混音器控件全部无效**，音量必须全程软件实现

> 2026-11-02 实测推翻了我最初写这一节时的假设。下面是实测结论，不是推理。

#### 4.5.1 实测结果

用 `s1k.wav`（1000 Hz 正弦，PCM 峰值 **0.17633 FS**）播放，8 通道 S32 录制，
用 Goertzel 精确测量 1 kHz 分量幅度：

| 实验 | 改动 | ch7 幅度 | 相对基准 | 麦克风均值 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `S_base` | 基线（DAC=251） | 0.17585 | **0.00 dB** | 0.00 dB | 参考 |
| `S_vol247` | DAC Volume 251→247 | 0.17600 | +0.01 dB | −0.00 dB | ❌ 无效 |
| `S_vol243` | →243 | 0.17586 | +0.00 dB | −0.01 dB | ❌ 无效 |
| `S_vol235` | →235 | 0.17594 | +0.00 dB | −0.01 dB | ❌ 无效 |
| `S_vol215` | →215 | 0.17603 | +0.01 dB | +0.01 dB | ❌ 无效 |
| `S_vol175` | →175 | 0.17581 | −0.00 dB | −0.02 dB | ❌ 无效 |
| `S_vol100` | →**100** | 0.17595 | **+0.00 dB** | −0.00 dB | ❌ 无效 |
| `T_extra12db` | Extra Gain 0dB→**12dB** | 0.17550 | −0.02 dB | −0.00 dB | ❌ 无效 |
| `T_extra18db` | 0dB→**18dB** | 0.17573 | −0.00 dB | — | ❌ 无效 |
| `X_soft255` | mysoftvol 60→**255** | 0.17566 | −0.01 dB | — | ❌ 无效 |
| `X_soft120` | mysoftvol→120 | 0.17558 | −0.01 dB | — | ❌ 无效 |
| `X_soft0` | mysoftvol→**0** | 0.17560 | **−0.01 dB** | — | ❌ 无效 |

`DAC Digital Playback Volume` 从 **251 一路降到 100**（151 档 × 3.75 dB = 名义 **566 dB**），
`mysoftvol` 从 60 拉到 255 再压到 **0**，`DAC Extra Digital Gain` 从 0dB 加到 **18dB** ——
**三种改动对输出幅度的影响都在 ±0.02 dB 以内，即完全没有影响。**

`Ext Spk Switch` on/off 也只带来 0.4 dB 的麦克风侧差异（属于测量重复性误差），
说明该控件同样没有实际接入信号通路。

#### 4.5.2 ch7 是**数字环回**，不是模拟回采

这一条直接改写了 §1 的物理模型：

| 证据 | 实测值 | 含义 |
| --- | --- | --- |
| ch7 幅度 / PCM 幅度 | **−0.02 dB** | 单位增益，即**未经任何衰减**的原样拷贝 |
| ch7 峰值 vs PCM 峰值 | `0.249359` vs `0.249359` | **逐样本相同** |
| ch7 静音段 | **恰好 0**（−240 dB） | 数字零，不是模拟噪声底（模拟至少 −100 dB） |
| 不受任何混音器控件影响 | 见上表 | 不经过 DAC 模拟域 |
| 与源 R 声道对齐残差 | −24.5 dB @ shift=14 | 非比特级，说明经过一次 S16 中间格式转换 |
| 与源 L 声道对齐残差 | −12.1 dB | 不匹配 |

**结论**：`ch7` 是写入播放 PCM 的**数字拷贝**（输出点位于 `S16 → 硬件 S32` 转换之后，
因为残差 −24.5 dB 恰好像一次 16→32 位移 + 量化）。

**对 AEC 的影响（正面）**：

```
✅ 参考信号绝对纯净：零声学泄漏、零功放非线性、零 ADC 噪声
✅ 与麦克风在同一 ALSA buffer，帧对齐误差 = 0
✅ 不受音量影响 → 不需要"音量纪律"，滤波器系数永远有效
❌ 参考里不含功放/扬声器的非线性失真 → AEC 无法消除这部分残余
   （Speex MDF 是线性自适应滤波器，非线性失真本来也消不掉；影响次要）
```

> 原 §4.5 担心的「小音量时参考掉进噪声底」**不存在**——参考永远是满幅数字拷贝。
> 但反过来，**输出音量只能靠软件做**，因为硬件不给任何衰减手段。

#### 4.5.3 ⚠️ 新增硬约束：只能写**右声道**，左声道会被丢弃

用双频立体声文件测试（L=1 kHz，R=3 kHz）：

| 源文件 | 听/录到什么 |
| --- | --- |
| L=1kHz, R=3kHz | 一切正常，但**只有 3kHz**（L 完全丢失） |
| L=3kHz, R=1kHz | **只有 1kHz**（即始终是源的 R 声道） |
| **L=1kHz, R=静音** | **完全无声**（ch7 全 0，麦克风无回声） |
| `Output Swap` = R/L、L/L | **无任何变化** |

**硬件只播放立体声流的 index 1（右声道），并把同一路送到两个喇叭。**
`Output Swap` 控件无效。`Lineout *` 四个枚举开关切换也无变化。

→ **下行写入必须把单声道 PCM 复制到两个声道，或者直接把每个样本写两遍。**
绝不能只写 index 0，否则**完全没有声音**。

```c
/* 正确：mono sample m → [m, m] */
for (i = 0; i < n; i++) { out[2*i] = m[i]; out[2*i+1] = m[i]; }
```

#### 4.5.4 音量正确做法

硬件层面**没有任何可用衰减**，所以：

```
1. 不再设置任何 amixer 音量控件（设了也没用，只会误导后来接手的人）。
2. 音量全程在 ⑨ 里对 PCM 做软件乘性缩放：
     sw_gain = 10^(vol_db / 20)
     out[i] = clamp(pcm[i] * sw_gain)
3. 软件音量下限保护：低于 -30 dB 直接送 0（静音），不要送极小幅度信号。
4. 需要更细的纹波控制时，用 dithering（TPDF）避免 16 bit 量化的零点失真。
```

因为 ch7 是数字拷贝，软件衰减后 ch7 与麦克风拾音**同比例下降**，
AEC 的滤波器系数始终有效 —— 这正是我们希望的不变量，而且**天生满足，无需纪律约束**。

---

### 4.6 ④ 重采样 48k → 16k（在 AEC **之前**做）


**关键设计决策：先降采样，再 AEC。**

| 方案 | 滤波器长度 | 相对算力 | 结论 |
| --- | --- | --- | --- |
| AEC 跑 48k | 9600 taps（200ms） | **9×** | ❌ 4×A53 上风险大 |
| AEC 跑 16k（**选这个**） | 3200 taps（200ms） | 1× | ✅ |

理由：

1. AEC 算力与 `filter_length × 通道数` 成正比，48k 下 taps 是 16k 的 3 倍，**FFT 尺寸也是**，
   实际开销是 **9 倍**（3× taps × 3× FFT 长度，虽然 FFT 是 O(n log n)，但 MDF 的分段数直接 ×3）。
2. 语音能量 99% 在 4kHz 以下，16k 采样（8k 带宽）对 ASR 完全够。
3. SpeexDSP 的 AEC 本来就是为 16k 设计的，48k 是超范围使用。

**实现**：`speex_resampler_init(1, 48000, 16000, quality=7, &err)`，`3:1` 整数倍，
定点相位、组延迟确定。**7 路（6 mic + 1 ref）必须用完全相同的初始化参数**，
并且**每次调用处理相同的样本数**（480 → 160），这样 7 路之间的相对延迟严格为 0，
AEC 的对齐不会被重采样打破。

> ⚠️ 不要用 ALSA 的 `speexrate` 插件做这件事：它只能作用于整条 PCM，
> 而我们只需要 ref 通道重采样后与 mic 通道**逐样本对齐**，插件层管不了这个约束。

### 4.7 ⑤ SpeexDSP MC-AEC 参数

```c
SpeexEchoState *aec = speex_echo_state_init_mc(
    /* frame_size    */ 160,     // 10ms @16k
    /* filter_length */ 3200,    // 200ms @16k，必须是 frame_size 的整数倍
    /* nb_mic        */ 6,
    /* nb_speakers   */ 1
);
speex_echo_ctl(aec, SPEEX_ECHO_SET_SAMPLING_RATE, &rate /*16000*/);
```

**`filter_length = 3200` 是怎么来的**：

```
残留声学延迟（ref → mic）上限    ≈ 60 ms     （实测包络滞后最大 +64ms，取上界）
房间/箱体混响尾巴 (T60 ≈ 0.3s)   ≈ 100 ms    （取 -30dB 处）
安全余量                          ≈ 40 ms
──────────────────────────────────────────
合计                               200 ms = 3200 samples @16k
```

如果 CPU 超预算，退到 **2400（150ms）**；再不行退到 **1600（100ms）**，代价是混响尾巴消不干净。

**接口约定**（Speex MDF 的 MC 模式）：

```
in[frame*6]  ← 6 路 near，**按帧交错**：L0..L5, L0..L5, ...（每个时刻 6 个样本连续）
far_end[frame] ← 1 路 ref，连续
out[frame*6]  ← 6 路消回声后输出，同交错格式
```

**为什么用 `_mc` 而不是 6 个独立 state**：
MC 版本对 `far_end` 只做一次 FFT（6 路共享），省掉 5 次 FFT/帧，并且
6 路滤波器在同一个参考上联合自适应，收敛更一致。CPU 上大约省 20~25%。

**内存估算**（每路 MDF state）：

```
M = 3200/160 = 20 段
W (滤波器)    20 × 160 × 4B × 2(复)      ≈ 102 KB
X (参考谱)    20 × 512 × 4B × 2(复)      ≈ 82 KB
power/其他                                  ≈ 20 KB
每路合计                                  ≈ 205 KB × 6 ≈ 1.2 MB
```

完全在预算内。

### 4.8 ⑥ 通道选择（V1）/ 波束成形（V2）

#### V1：按 post-AEC 质量选一路（**先做这个**）

AEC 每帧对 6 路都产出结果。选择依据用**滑窗统计**，不要用瞬时值：

```
对每路 m：
  rms_post[m]  = 消回声后 200ms 滑窗 RMS
  corr[m]      = 消回声后信号与 ref 的归一化互相关（200ms 窗）
  score[m]     = rms_post[m] × (1 - |corr[m]|)
选 score 最大的一路。
```

`(1 - |corr|)` 是「回声消除彻底程度」的代理：残差与参考越不相关，说明消得越干净。
每 **500ms** 更新一次选择，切换时做 **20ms 交叉淡化**，避免 ASR 侧听到咔哒。

同时把 `score[]` 和 `corr[]` 全部打进 `/stats`，方便事后判断是不是某一路麦克风坏了。

> 为什么不用「固定 ch3」：麦克风灵敏度个体差异 + 用户摆放姿态会让最优通道漂移。
> 而且这个统计几乎不花算力（复用已有的 RMS）。

#### V2：GCC-PHAT + delay-and-sum（**确认 V1 效果后再做**）

```
1. 对 ch_i / ch_j 做 50% 重叠 512 点 STFT（窗长 32ms @16k）
2. 互谱 × 相位归一化：G_ij(f) = X_i(f) · conj(X_j(f)) / |X_i · conj(X_j)|
3. 逆变换取峰值 → TDOA_ij（|TDOA| ≤ 11 samples）
4. 以 score 最高的一路为基准，其余按 -TDOA 做整数延迟对齐
5. 加权求和（权重 = post-AEC RMS）
```

**约束**：`|TDOA| ≤ 11 samples @16k`（约 0.7ms，对应 6 麦最大口径）。
如果算出来超过这个值，说明峰值抓到了混响而不是直达声 → **本帧弃用，回退 V1**。
这一条是 §1.1 那个纠偏的落地形式。

### 4.9 ⑦ 预处理（降噪）

```c
SpeexPreprocessState *pp = speex_preprocess_state_init(160, 16000);
speex_preprocess_ctl(pp, SPEEX_PREPROCESS_SET_ECHO_STATE, aec);   // 必须
int on = 1,  off = 0;
speex_preprocess_ctl(pp, SPEEX_PREPROCESS_SET_DENOISE, &on);      // 开
speex_preprocess_ctl(pp, SPEEX_PREPROCESS_SET_AGC,     &off);     // **关**
speex_preprocess_ctl(pp, SPEEX_PREPROCESS_SET_VAD,     &off);     // **关**
```

- **AGC 必须关**：AGC 会在静音段把增益推到最大，把底噪放大成「呼吸声」，
  直接破坏 Mac 侧 `jarvis/bargein.py` 的 12dB 能量基线，也会让 sherpa KWS 误触发。
- **VAD 必须关**：Mac 侧已经有 `silero-vad`（`jarvis/vad.py`），做两遍 VAD 只会互相打架。
- `SET_ECHO_STATE` 必须设：让预处理器知道哪些频点被回声占了，降噪时避开。

### 4.10 ⑧ 上行输出

```
格式：16000 Hz / mono / S16_LE / 小端
速率：32000 B/s（256 kbps）
时机：每 10ms 一帧，攒够 2 帧（20ms）发一次，减少包开销
```

**不做编码压缩**。Opus 需要额外库、增加 20ms 编码延迟，而 256kbps 在局域网毫无压力。
将来要走公网再接 Opus。

---

## 5. 下行：Mac → 播放 → ch7

### 5.1 ⑨ 抖动缓冲

```
target_depth = 30 ms（3 帧）
自适应范围  = 20 ~ 80 ms
策略：
  - 持续 underrun → depth += 10ms（上限 80ms）
  - 连续 5s 无 underrun → depth -= 5ms（下限 20ms）
```

用环形缓冲区（SPSC lock-free，`head/tail` 原子变量），
上行线程写、播放线程读，不持锁。

### 5.2 ⑨ 软件音量（**唯一**的音量手段）

在**写入环形缓冲之前**乘系数（float 域）：

```
sw_gain = 10^(vol_db / 20)
out[i] = clamp(pcm[i] * sw_gain)
```

- 硬件**没有任何可用衰减**（§4.5.1：四个控件全部无效），所以软件音量不是"备选"，是**唯一路径**
- 音量变化只影响 PCM 幅度 → ch7 数字环回同比例变化 → 麦克风拾音同比例变化
  → AEC 的滤波器系数**始终有效**
- 下限保护：低于 **-30 dB** 直接送全 0，不要送极小幅度信号（避免 16 bit 量化零点失真被 AGC 放大）
- 需要更细的纹波控制时加 TPDF dithering

`FLUSH` 消息到达时：清空环形缓冲 + 向 ALSA 写一个 period 的静音 + `snd_pcm_drop`。

### 5.3 ⑩ 重采样 16k → 48k

`speex_resampler_init(1, 16000, 48000, quality=7)`，`1:3` 上采样。
每 160 样本输入 → 480 样本输出，正好一个 ALSA period。

上采样后的信号带宽 8kHz，与本地上行链路的带宽一致，
所以 ch7 参考与麦克风听到的内容在**频带上严格对齐**——AEC 不会因为
「参考里有 12kHz 但麦克风听不到」而在高频乱收敛。这是选 16k 全链路的一个额外好处。

### 5.4 ⑪ ALSA 播放

```
PCM  hw:0,0 (pcmC0D0p)
fmt  S16_LE, 2ch（**左右必须完全相同** —— 见下）
rate 48000
period 480 帧（10ms）
buffer 1440 帧（30ms）
```

#### ⚠️ 硬约束：硬件只播放右声道

实测（§4.5.3）：

| 写入内容 | 结果 |
| --- | --- |
| L=1kHz, R=3kHz | 只听到/录到 **3kHz**，1kHz 完全消失 |
| L=3kHz, R=1kHz | 只听到 **1kHz** |
| L=1kHz, R=静音 | **完全无声** |
| `Output Swap` 任意值 | 无变化 |

→ **单声道 PCM 必须复制到两个声道**，每个样本写两遍：

```c
/* 正确 */
for (i = 0; i < n; i++) { out[2*i] = m[i]; out[2*i+1] = m[i]; }

/* 错误：写 index 0 会完全没声音（且 AEC 因为 ch7 静音而无法收敛） */
```

这也解释了一个容易踩的坑：如果只写左声道，现象是「进程正常、ALSA 无报错、
`/proc/asound/.../status` 显示 RUNNING，但**完全没有声音**」——
调试时先怀疑声道索引，不要怀疑功放或混音器。

其他要点：

- `snd_pcm_prepare()` 后持续 `writei`
- underrun 时 `snd_pcm_prepare()` 重来，**不要**直接 `snd_pcm_start`
- 无数据时**必须写静音**，不能停流：停流会让 ch7 变 0，
  下次播放时 AEC 需要重新收敛（虽然数字环回下收敛很快，但仍有瞬态）

### 5.5 打断（barge-in）的端到端时序

这是全双工方案真正要兑现的东西：

```
t=0     Mac 检测到用户说话（barge-in 触发）
t≈0     Mac 发 FLUSH 帧（8 字节）
t≈2ms   设备收到 → 清环形缓冲 → snd_pcm_drop
t≈12ms  DAC 输出静音 → ch7 ≈ 0
t≈15ms  声学回声消失 → mic 里只剩用户声音
t≈20ms  上行 16k 帧到达 Mac
```

**总打断延迟 ≈ 20~40 ms**（不含 WiFi）。相比现在 `TTS_TARGET=speaker` 走米家云
（完全不能打断），这是质变。

> 注意：`jarvis/audio.py::Speaker.stop()` 现在的语义是 `stream.abort()`。
> 端点版必须实现等价的 `stop()` → 发 FLUSH。接口一致，`app.py` 不用改。

---

## 6. 网络协议

### 6.1 端口

| 端口 | 协议 | 用途 |
| --- | --- | --- |
| `8765` | TCP | 双向音频 |
| `8766` | HTTP | `/health` `/stats` `/config`（只读为主） |

**为什么用 TCP 不用 UDP**：局域网、单跳、无需抗丢包；TCP 的丢包重传 + 我们的小抖动缓冲
足够覆盖 WiFi 抖动，且省掉一整套 RTP/序列号/重排逻辑。

**为什么不用 WebSocket**：设备侧要在 C 里实现握手 + SHA1 + base64 + 掩码帧，
纯属给自己找事。原始 TCP 完全够。Mac 侧是 Python，实现成本同样低。

> 上游 §16.8 预留了 `SPEAKER_WS=ws://...`。这里把值改成 `tcp://192.168.1.3:8765`，
> 变量名保留（`AUDIO_SOURCE=speaker_endpoint` 不变），避免改动已有约定。

### 6.2 上行（设备 → Mac）：裸 PCM

```
无帧头。连接建立后立即开始，16000 Hz / mono / S16_LE，连续不断。
```

理由：上行永远只有音频，加帧头没有收益；断流由 TCP 保序保证，
重连后从下一个 10ms 帧开始即可（音频里偶尔缺一段不影响 VAD/ASR）。

### 6.3 下行（Mac → 设备）：8 字节帧头

```c
struct frame_hdr {          // 小端
    uint8_t  type;          // 1=PCM 2=FLUSH 3=VOLUME 4=PING
    uint8_t  flags;         // bit0 = 该段之后还有后续（用于 TTS 分段）
    uint16_t reserved;      // 0
    uint32_t length;        // payload 字节数（PCM 必须是 320 的整数倍 = 10ms）
};
```

| type | payload | 行为 |
| --- | --- | --- |
| `1` PCM | 16k mono S16LE | 写环形缓冲 |
| `2` FLUSH | 空 | 清空缓冲 + 播放静音 + `snd_pcm_drop` |
| `3` VOLUME | 1 字节（0=-60dB .. 100=0dB） | 更新软件音量系数 |
| `4` PING | 空 | 回 `PONG`（走 HTTP `/stats` 里体现 RTT） |

**为什么下行要帧头而上行不要**：控制消息必须有带内通道，否则 FLUSH 只能靠关连接
（重连 20~50ms，且 TTS 分句播报会频繁重连）。8 字节开销对 320 字节的 PCM 是 2.5%，可接受。

---

## 7. 与 home-jarvis 的对接契约

### 7.1 环境变量

```dotenv
AUDIO_SOURCE=speaker_endpoint
SPEAKER_ENDPOINT=tcp://192.168.1.3:8765
SPEAKER_CTL=http://192.168.1.3:8766
```

### 7.2 Mac 侧需要新增的两个类（鸭子类型对齐现有接口）

`jarvis/audio.py` 里 `Mic` 的对外接口是 `read(timeout) -> np.ndarray|None` / `drain()`，
`Speaker` 是 `play(pcm, rate, blocking)` / `stop()` / `playing`。
新类**必须一模一样**，这样 `app.py` 与 `bargein.py` 一行不改：

```python
class EndpointMic:
    """网络麦克风：接口与 Mic 完全一致（read/drain/record）。"""
    def __init__(self, url: str) -> None: ...
    def read(self, timeout: float = 1.0) -> np.ndarray | None: ...
    def drain(self) -> None: ...


class EndpointSpeaker:
    """网络喇叭：接口与 Speaker 完全一致（play/stop/playing）。"""
    def __init__(self, url: str, ctl_url: str) -> None: ...
    def play(self, pcm: np.ndarray, sample_rate: int,
             blocking: bool = True) -> threading.Thread | None: ...
    def stop(self) -> None: ...   # 发 FLUSH
```

关键点：

- `EndpointSpeaker.play()` 内部把任意 `sample_rate` 用 `soxr` 重采样到 **16k** 再发
  （TTS 可能是 22050/24000）
- `EndpointSpeaker.stop()` 发 FLUSH，然后**立即返回**（不能等设备确认，否则打断变慢）
- `EndpointMic` 自带重连（1s 退避），断线时 `read()` 返回 `None`，
  上层现有的「超时返回 None」逻辑天然兼容

### 7.3 `config.py` 需要的改动

```python
audio_source: str = "local"              # local | speaker_endpoint
speaker_endpoint: str = ""               # tcp://<ip>:8765
speaker_ctl: str = ""                    # http://<ip>:8766
```

`Settings.load()` 里加三行 `_env()`，其余不动。

### 7.4 barge-in 阈值需要重新标定

`jarvis/config.py` 现在是 `barge_in_margin_db = 12.0`，这是「半双工 + 无 AEC」时代的保守值。

AEC 生效后，播放期间麦克风里的回声残差应该比语音低 **15~25 dB（ERLE）**，所以：

```
预计可下调到 6~9 dB
```

**但要实测**：先在 Mac 上用 `/stats` 里的 `erle_db` 统计 10 次播报的分布，
取 `erle_db` 的 20 分位再减 3dB 作为 `barge_in_margin_db`。
不要凭感觉调——调太低会在播报中自触发，调太高则打断不灵敏。

---

## 8. 预算

### 8.1 CPU（估算，**必须先实测**）

单核 1.5GHz Cortex-A53，标量 C 代码实际吞吐按 **0.6 GFLOP/s** 估。

| 环节 | 每 10ms 帧开销 | 稳态占用 |
| --- | --- | --- |
| 反交错 + S32→float + HPF ×7 | ≈ 30 kFLOP | 3 MFLOP/s |
| 重采样 48→16 ×7（Q7，≈60 taps） | ≈ 0.67 MFLOP | 67 MFLOP/s |
| **MC-AEC 6mic × 3200 taps** | **≈ 1.3 MFLOP** | **130 MFLOP/s** |
| 预处理（1 路） | ≈ 40 kFLOP | 4 MFLOP/s |
| 重采样 16→48 + 播放写 | ≈ 0.15 MFLOP | 15 MFLOP/s |
| **合计** | | **≈ 220 MFLOP/s ≈ 单核 35%** |

结论：**放得下**，但要占满将近半核。因此：

- 采集+DSP 线程设 `nice -10`（或 `SCHED_FIFO` 优先级 10），
  避免被 `mipns`/`dropbear` 抢占导致 ALSA overrun
- **P0 落地第一件事是在设备上跑 benchmark**（§11.2），实测数字替换本表

**降级阶梯**（如果实测超 60% 单核）：

| 步骤 | 操作 | 省下的算力 | 代价 |
| --- | --- | --- | --- |
| 1 | `filter_length` 3200 → 2400 | -25% | 混响尾巴消不干净 |
| 2 | AEC 只跑 2 路（预选 RMS 最高的 2 路） | -67% | 丢失通道冗余 |
| 3 | 关掉 4 路的 AEC，只做 DC+增益后参与波束 | -50% | V2 波束质量下降 |

### 8.2 内存

| 项 | 估算 |
| --- | --- |
| 6 路 MDF state | 1.2 MB |
| 7 路 resampler（48↔16） | < 100 KB |
| ALSA mmap buffer（采集 50ms + 播放 30ms） | ≈ 160 KB |
| 抖动缓冲（80ms 上限） | ≈ 3 KB |
| 线程栈 ×3 | 3 MB |
| **合计** | **≈ 5 MB** |

`MemAvailable 141MB` → 完全宽裕。

### 8.3 端到端延迟

| 段 | 延迟 |
| --- | --- |
| ALSA 采集 period（10ms，均值） | 5 ms |
| 解交织 + HPF + 重采样 | < 1 ms |
| AEC 帧对齐（10ms） | 10 ms |
| 上行攒帧（20ms 一次，均值） | 10 ms |
| WiFi（TCP，局域网） | 5~15 ms |
| **上行新增合计** | **≈ 30~40 ms** |

| 段 | 延迟 |
| --- | --- |
| WiFi | 5~15 ms |
| 抖动缓冲 | 30 ms |
| 重采样 16→48 | < 1 ms |
| ALSA 播放 period（10ms，均值） | 5 ms |
| **下行新增合计** | **≈ 40~50 ms** |

对比现在 `TTS_TARGET=local`（`sounddevice` 直连，≈0ms 额外）与
`TTS_TARGET=speaker`（米家云 TTS，≈1000ms + 不可打断）：

```
端点方案：TTS 首字延迟 +40~50ms，但换来「全屋可听 + 可打断」
```

这个交换是划算的。

---

## 9. 交叉编译与部署

### 9.1 目标约束

```
arch   aarch64
libc   glibc 2.19（设备现役）—— 新工具链产物会直接 GLIBC_2.28 not found
kernel 3.14.29
依赖   无 gcc / 无 python / 无 pkg-config
```

### 9.2 方案对比

| 方案 | 做法 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A（推荐）全静态 musl** | `zig cc -target aarch64-linux-musl -static` + 自带 speexdsp + **直接 ioctl 访问 `/dev/snd`** | 零 ABI 风险、单文件 2MB、不依赖设备任何 .so | 需要自己写约 200 行 ALSA ioctl 封装 |
| B 动态 glibc 2.19 | 用 glibc 2.19 sysroot 编译，链接设备 `libasound.so.2` + `libspeexdsp.so.1.5.0` | 复用厂商 DSP 库、代码少 | 需要老 sysroot（Ubuntu 14.04 arm64 / crosstool-ng），工具链极难搭 |
| C 全静态 glibc | 静态链 glibc 2.19 | — | glibc 静态链接有 dlopen/NSS 警告，且同样要老 sysroot |

**选 A。** 理由：

1. 我们**不需要** ALSA 插件系统（`plughw`/`speexrate`/`dmix` 全部不用），
   只需要 `hw:0,0` 的 raw PCM ioctl。`SNDRV_PCM_IOCTL_HW_PARAMS` 这套 ABI 从 2.6 到 6.x 稳定，
   自己封装比引入 alsa-lib 更省事。
2. 静态二进制不受设备 `libc 2.19` 限制，也不怕将来厂商 OTA 改库。
3. `zig cc` 一条命令就能产出 aarch64-musl 静态二进制，不需要装交叉工具链。

### 9.3 构建

```bash
# 依赖（都很小）
#   speexdsp 1.2.1 源码  → 静态库
#   endpoint.c            → 主程序

zig cc -target aarch64-linux-musl -O2 -mcpu=cortex_a53+neon \
    -static -o s12audio \
    endpoint.c \
    deps/speexdsp/libspeexdsp.a \
    -lpthread -lm
```

产物预期 **1.5~2.5 MB**，`/` 剩余 46.9M 装得下。

### 9.4 直接 ioctl 需要覆盖的最小子集

```
open("/dev/snd/pcmC0D0c", O_RDWR)
ioctl(SNDRV_PCM_IOCTL_INFO)
ioctl(SNDRV_PCM_IOCTL_HW_PARAMS)      // 8ch/S32/48k/period 480/buffer 2400
ioctl(SNDRV_PCM_IOCTL_SW_PARAMS)
ioctl(SNDRV_PCM_IOCTL_PREPARE)
ioctl(SNDRV_PCM_IOCTL_START)
ioctl(SNDRV_PCM_IOCTL_DROP)
ioctl(SNDRV_PCM_IOCTL_MMAP)           // 或退化为 READI/WRITEI
```

用 `READI`/`WRITEI`（阻塞 read/write）实现最简单，代价是一次内核拷贝（15360 B/10ms，
约 1.5 MB/s，CPU 开销可忽略）。**V1 就用 `READI/WRITEI`**，不要一上来就 mmap。

> 若 zig 方案在设备上遇到问题，回退：用 `gcc-aarch64-linux-gnu` + Ubuntu 14.04 arm64 sysroot
> 走方案 B，或者干脆先用 `arecord | nc` 做 PoC（§12 Stage A）。

### 9.5 部署与开机自启

```
/data/s12audio/s12audio          # 二进制（/ 空间紧张，放大分区）
/data/s12audio/endpoint.conf     # 增益/音量等标定值
/etc/init.d/S99s12audio          # SysV 启动脚本（该机型用 inittab/rcS，不是 systemd）
```

启动脚本要做的**顺序**：

```sh
#!/bin/sh
case "$1" in
  start)
    /etc/init.d/pns stop                       # ① 唯一真正必需的一步：释放 hw:0,0
    /data/s12audio/s12audio -c /data/s12audio/endpoint.conf &
    ;;
  stop)
    killall s12audio
    /etc/init.d/pns start                      # 回滚：恢复原厂语音
    ;;
esac
```

> **不要再写 `amixer` 音量设置**。实测全部无效（§4.5.1），写了只会误导后来接手的人。
> 混音器保持出厂值即可——它不影响播放，也不影响采集。

> ✅ 唯一要确认的是混音器**没有被别人改坏**：`amixer cget numid=3` 应为 `off`。
> 即便如此也不急——`Audio i2s mute` 实测同样无效，写 off 只是保持基线一致性。

**看门狗**：`pns` 停了之后如果我们的进程挂了，音箱就彻底哑了。所以：

```
主进程 fork 一个 watchdog 子进程，每 2s 通过 pipe 收一次心跳；
连续 3 次没收到 → 杀掉主进程重新 exec；
重启 5 次仍失败 → 执行 /etc/init.d/pns start 回滚到原厂状态。
```

这条是「别把家里的音箱搞成砖」的保险。

---

## 10. 配置表（`endpoint.conf`）

| key | 默认 | 说明 |
| --- | --- | --- |
| `capture_dev` | `/dev/snd/pcmC0D0c` | — |
| `playback_dev` | `/dev/snd/pcmC0D0p` | — |
| `period_frames` | `480` | 10ms @48k，与 AEC 帧对齐，**别改** |
| `filter_ms` | `200` | AEC 滤波器长度（ms），可选 100/150/200 |
| `hp_fc_hz` | `80` | DC 阻断截止频率 |
| `gain_mic` | `56.0` | 麦克风固定增益（线性倍数），2026-11-02 重标定；**勿用 265** |
| `gain_ref` | `1.0` | 参考增益，**固定为 1.0**，不要放大（§4.4） |
| `resample_quality` | `7` | 0~10，越高越慢 |
| `ns_enable` | `1` | 预处理降噪 |
| `agc_enable` | `0` | **必须 0** |
| `preprocess_vad` | `0` | **必须 0**（Mac 侧有 silero） |
| `channel_select_ms` | `500` | 通道选择更新周期 |
| `jitter_target_ms` | `30` | 下行抖动缓冲目标深度 |
| `net_port` | `8765` | 音频 TCP |
| `ctl_port` | `8766` | HTTP 控制 |
| `sw_volume_db` | `0` | 软件音量（Mac 可通过 VOLUME 帧覆盖） |

---

## 11. 试验与验收

### 11.1 设备自检（`s12audio --selftest`）

启动必须过的 5 项，任何一项失败就**不接管 pns**：

```
[1] ch6 静音检查      ：3 秒内 ch6 |max| < 1e-4 FS
[2] ch7 有效性检查    ：播放已知单声道测试音（复制到双声道），
                         ch7 与写入的 PCM 归一化互相关 > 0.999 且幅度比在 0.98~1.02
[3] **ch7 纯净度检查** ：播放前 200ms 静音段，ch7 |max| 必须**恰好为 0**
                         （若有底噪 → 说明 ch7 不是数字环回，AEC 的"零噪声参考"假设失效）
[4] 麦克风电平检查    ：ch0-5 中至少 3 路 RMS > -55 dBFS
[5] DC 偏置检查       ：ch0-5 偏置在 -0.02 ~ -0.10 FS 区间（预期 -0.05 附近）
```

**再加一条写路径断言**（比自检更重要，因为它防的是"没声音"这种静默失败）：

```
[0] 播放前先写 200ms 已知幅度（-12 dBFS）的 1kHz 测试音到**两个声道**，
    回读 ch7 确认幅度匹配。若 ch7 全 0 → 立刻报警退出，不接管 pns。
    典型原因：只写了左声道（§4.5.3）。
```

这一套就是 §16.9 那些分析脚本的**固化版**，每次启动都跑，
这样换了机器/换了固件也能立刻发现「通道顺序变了」或「ch7 性质变了」。

### 11.2 AEC 效果验收

| 指标 | 方法 | 目标 |
| --- | --- | --- |
| **ERLE（回声抑制量）** | 播 `-12 dBFS` 白噪声 + 麦克风侧静音，比较 AEC 前后同频带 RMS | **≥ 20 dB**（1kHz 倍频程） |
| **双讲不失真** | 播放中由人说话，测消回声后语音 RMS 相对无播放时的衰减 | **≥ -3 dB**（不能明显吃掉近端） |
| **收敛时间** | 从开始播放到 ERLE 稳定在 15dB 以上 | **≤ 1.5 s** |
| **ASR 词错率** | 播报中打断说话，SenseVoice 识别 CER | 相对无回声场景 **≤ +5 个百分点** |
| **打断延迟** | Mac 发 FLUSH → 设备 `/stats` 音量计归零 | **≤ 15 ms** |
| **长稳** | 连续 2 小时播报 + 对话 | 无 overrun、内存不涨 |

**ERLE 测量脚本**建议放在 `scripts/aec_bench.py`（Mac 侧）：
从 `/stats` 拿设备侧 `rms_in` / `rms_out` 的逐帧值，直接算
`ERLE = 20*log10(rms_in / rms_out)`，按倍频程分开统计。

### 11.3 端到端验收（对齐 §16.8 的清单）

- [ ] `AUDIO_SOURCE=speaker_endpoint` 下，「唤醒 → 家电控制 → 语音回复」全链路通
- [ ] 播报过程中说唤醒词，能打断（不依赖 `barrage` 定时器）
- [ ] 调 Mac 侧音量，AEC 效果不变（ch7 数字环回 → 比例恒定，§4.5.2）
- [ ] **写左声道 / 只写单声道时，自检必须报警退出**（§11.1 第 [0] 项，防静默无声）
- [ ] 断网重连后 5 秒内自动恢复上行
- [ ] 断电冷启动 → 30 秒内端点可用
- [ ] `killall s12audio` → 5 次重启失败后自动回滚到原厂语音（不会变砖）

---

## 12. 分阶段落地

| 阶段 | 做什么 | 产出 | 风险 |
| --- | --- | --- | --- |
| **A. 打通采集** | 设备 `arecord -f S32_LE -c 8 -r 48000 \| nc` → Mac 存 wav，numpy 拆通道 | 确认 8 通道数据在 WiFi 上稳定回传 | 8ch S32 = **1.5 MB/s**，WiFi 可能吃力，仅作 PoC |
| **B. Mac 上先跑 AEC** | 用 `speexdsp-python` 在 Mac 上做 §4.3~4.7，调 `filter_ms` / `gain` | **ERLE 曲线、最优参数、选哪一路** | 无（算法验证，不碰设备） |
| **C. 设备守护进程** | 交叉编译 §9，实现 §4 上行 + §6.3 下行控制 | `s12audio` 二进制 + init 脚本 | 交叉编译 + ioctl 是唯一硬骨头 |
| **D. 接 jarvis** | `EndpointMic` / `EndpointSpeaker` + `config.py` 三行 | 全链路可用 | 低 |
| **E. 调优** | barge-in 阈值、通道选择、长稳 | 可日常使用 | 低 |
| **F. 波束成形** | §4.8 V2 | 远场提升 | 可选，收益不确定 |

**关键建议：B 必须在 C 之前。**
AEC 效果的成败取决于 §4.4 的增益标定，这个参数在 Mac 上调最方便
（numpy 随便改、随时出图）。等参数定了再写 C，能省掉「改一次参数重新交叉编译一次」的地狱循环。

> 好消息：§4.5 的实测把「音量纪律」这个不确定性**整个消掉了**——
> ch7 是数字环回，与音量无关，滤波器系数永远有效。
> B 阶段只剩一个真正的自由度：`gain_mic` 和 `filter_ms`。

**Stage A 的替代方案**：如果 1.5MB/s 的 8 通道裸流传不动，退一步用
`arecord -f S32_LE -c 8 -r 48000 -d 5` 只录**短片段**（配合 `aplay` 播放已知信号），
拿来做参数标定用，不做连续流。参数标定本来也不需要连续流。

---

## 13. 风险与未验证项

### 13.1 已确认（可直接依赖）

- ✅ `libspeexdsp.so.1.5.0` 导出 `speex_echo_state_init_mc` / `speex_preprocess_run` / `speex_resampler_*`
- ✅ `hw:0,0` 采集 8ch S32 48k 与播放 2ch S16 48k 可**同时**打开（pns 停止后）
- ✅ **ch7 是播放 PCM 的数字环回**：幅度比 −0.02 dB、峰值逐样本相同、静音段恰好为 0（§4.5.2）
- ✅ **混音器四个音量控件全部无效**（DAC Volume / Extra Gain / mysoftvol / Output Swap）（§4.5.1）
- ✅ **硬件只播右声道**，`Output Swap` 无效（§4.5.3）
- ✅ ch6 恒为 0
- ✅ 4×A53 + NEON，内存 141MB 可用
- ✅ glibc 2.19，无编译器（交叉编译必须）
- ✅ 采集与播放共用音频 PLL（结构上无钟漂）

### 13.2 未验证（**不要当结论用**）

> 原本这里的第 1~3 条（音量控件、ch7 在 DAC 前后、功放/Lineout 组合）
> **已于 2026-11-02 全部验完**，结论转入 §4.5 / §13.1。

| # | 待验证 | 为什么重要 | 怎么验 |
| --- | --- | --- | --- |
| 1 | AEC 实际 CPU 占用 | 估算 35% 单核，可能偏差 2 倍 | 设备上跑 benchmark，`/proc/PID/stat` 前后对比 |
| 2 | ch0-5 的**真实 TDOA** | 决定 V2 波束成形可行性 | GCC-PHAT 在静音室/正常客厅各测一次，看是否 ≤ 11 samples |
| 3 | 播放 0 dBFS 满幅正弦时 ch7 是否削顶 | 若削顶，参考失真会让 AEC 学错 | 播 0dBFS 1kHz，看 ch7 峰值是否 > 1.0 FS |
| 4 | 长时间运行 `pcmC0D0c` 是否会被别的进程抢走 | 厂商 OTA 或看门狗可能重启 pns | 连续跑 2 小时，`ls -l /proc/*/fd` 查占用 |
| 5 | 写入 2ch 但**长度不对齐**（如 3 字节）时的行为 | 决定下行帧是否需要强制 4 字节对齐 | 构造异常帧，看 `writei` 返回 `-EINVAL` 还是静默截断 |

### 13.3 明确的边界

1. **不能保留小米原厂唤醒词** —— 接管 pns 后云端链路断开。home-jarvis 用自己的 KWS，这不是损失。
2. **不支持 5GHz / 远距离** —— 上行 256kbps + 下行 256kbps 很轻，但 WiFi 信号弱时 TCP 重传会抖，
   抖动缓冲会涨到 80ms。房间内正常距离无问题。
3. **不是高保真音乐端点** —— 全链路 16k，听音乐请别用这个通道。
4. **`libspeexdsp` 的 AEC 不是 NLMS 之王** —— Speex MDF 在双讲场景表现中等。
   如果实测 ERLE 达不到 20dB，备选是 WebRTC AEC3（需要自己交叉编译，但质量更好）。
   **先测再换**，别提前优化。

---

## 附录 A：与上游文档的关系

| 上游章节 | 本文的修正/深化 |
| --- | --- |
| §16.9 表 3「回声时延 -5~+64ms」 | ⚠️ **不能**用于波束成形（§1.1），物理上限只有 11 samples |
| §16.9 §5「AEC 实施方案」 | 深化为完整数据流（§3/§4），并修正为**先降采样再 AEC** |
| §16.9 §5「仍需自研 3 项」 | 给出具体做法：通道选择 §4.8、重采样 §4.6、交叉编译 §9 |
| §16.8「`SPEAKER_WS=ws://`」 | 改为 `tcp://`（§6.1），变量名保留 |
| §16.8「`SPEAKER_WS=ws://...`」验收清单 | 补齐为 §11.3 |
| §16.9「ch7 是电学参考信号（播放回路回采）」 | ⚠️ **修正为数字环回**：实测幅度比 −0.02 dB、静音段恰好 0、不受任何混音器影响（§4.5.2）。结论「可做 AEC 参考」不变，且**更强** |
| §16.9「ch7 谐波失真 -115 dB」 | 与「数字拷贝」一致——数字通路本来就没有谐波失真 |
| §16.9 未提及「只播右声道」 | ❗ **新增硬约束**：硬件只播 index 1，单声道必须复制双声道（§4.5.3） |

## 附录 B：踩过的坑（避免重复）

| 坑 | 现象 | 解法 |
| --- | --- | --- |
| 设备 `sleep 0.5` | `invalid number` | BusyBox sleep 不接受小数 |
| 精简 SSH 下 `cat`/`head` 找不到 | `command not found` | 用 `/bin/cat` 全路径 |
| 设备无 `od` | `od: not found` | 数据分析全部回传 Mac 做 |
| `scp` 不可用 | — | `cat file \| ssh 'cat > /path'` |
| `dropbear` 起不来 | `Bad buf_getptr` | host key 0 字节，`dropbearkey -t rsa -s 2048 -f ...` |
| `arecord` 报设备忙 | — | 先 `/etc/init.d/pns stop` |
| TTL 接线干扰供电 | 断电重插无启动声 | 只接 TX/RX/GND，不接 5V/3.3V |
| **设备 `/tmp` 是 117MB tmpfs** | 连续录 8ch 几次后 `arecord` 返回 1、文件不存在 | 每个 3s 录音占 4.6MB；**每轮录完立刻回传并 `rm`**，否则第 20 次左右爆盘（本次实测踩到） |
| **只写左声道** | 进程正常、ALSA 无报错、status=RUNNING，但**完全没声音**，且 AEC 无法收敛 | 硬件只播 index 1，单声道必须复制到两个声道（§4.5.3） |
| **按经验调 `amixer` 音量** | 调了半天音量毫无变化，误以为「功放坏了/驱动没加载」 | 该机型四个音量控件全部无效（§4.5.1），音量只能软件做 |
| 用白噪声 RMS 判断增益 | 结论在 -19.8 / -22.1 dBFS 两个值之间跳，呈非单调 | 白噪声 RMS 本身在 4s 窗内只稳定到 ±0.6dB，且无法区分「阵发性」；**改用稳态正弦 + Goertzel** 才得到 0.01dB 分辨率 |
---

## 附录 C：禁止系统自动更新（2026-11-02 已实施）

### C.1 为什么必须做

这台机器跑的是 `1.74.18`，而原厂 OTA 会**无条件覆盖**我们所有的接管成果：

| 会被抹掉的东西 | 位置 |
| --- | --- |
| root shell 免登录（`inittab` 改动） | `/etc/inittab` |
| dropbear 主机密钥与外网 SSH | `/etc/dropbear/`、`/etc/pam.d/common-auth` |
| 自建音频端点守护进程 | `/data/s12audio/`、`/etc/init.d/S99s12audio` |
| 本次的 OTA 拦截本身 | `/bin/ota`、`/bin/flash.sh` |

而且 **S12 没有版本比较保护**——`/bin/ota` 的 `version_check_no_need()` 里
`S12` 直接 `return 0`，意味着**只要服务器给一个 URL 就会烧写，不管版本号高低**。
换句话说，被推一个「降级包」也会照样接受。

### C.2 升级入口全景（实测）

```
① cron 每天 03:00       0 3 * * * /bin/ota slient         ← 静默检查
② 云端下发              ota check / matool_check_upgrade  ← 米家云
③ 米家 App 手动升级      ota upgrade <url> <md5> <size>    ← **绕过 matool，直接烧**
④ 蓝牙/局域网旁路        ota ble                            ← miio_helper / messagingagent / mico_aivs_lab
      ↓ 全部汇合于 ↓
⑤ /bin/ota  →  flash.sh <file>  →  board_system_upgrade()  →  reboot
```

**关键结论：`/bin/flash.sh` 是唯一汇合点。** 只要它拒绝，任何来源都无法烧写。

### C.3 已实施的三层拦截

| 层 | 措施 | 作用 |
| --- | --- | --- |
| 1 | 从 `/etc/crontabs/root` 与 `/var/spool/cron/crontabs/root` 移除 `0 3 * * * /bin/ota slient` | 消灭最常见的每日静默检查 |
| 2 | `/bin/ota` 替换为拦截器 | 封死 ①②③④ 所有 ota 子命令 |
| 3 | `/bin/flash.sh` 替换为拦截器 | 兜底⑤，即使有未知入口调用也无法烧写 |

拦截器（`/bin/ota-blocker.sh`，同一份内容部署到 `/bin/ota` 与 `/bin/flash.sh`）：

```sh
#!/bin/sh
LOG=/data/ota-blocked.log

_ota_blocked() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') BLOCKED [$1] args=[$2]" >> "$LOG"
    logger -t ota-blocked -p 4 "OTA blocked by policy: $1 $2"
}

case "$0" in
    */flash.sh)
        _ota_blocked "flash.sh" "$*"
        [ -n "$1" ] && [ -f "$1" ] && rm -f "$1"   # 删掉调用方传进来的包，避免重试
        exit 1
        ;;
    *)
        _ota_blocked "ota" "$*"
        exit 0                                      # 静默模式返回成功，避免被反复重试
        ;;
esac
```

设计要点：

- **`ota` 返回 0，`flash.sh` 返回 1**。`ota` 返回非 0 会让某些调用方（如 `mico_aivs_lab`）
  反复重试并可能触发 `upgrade_failed()` 里的 `reboot -f`；返回成功则安静退出。
  `flash.sh` 必须返回非 0，因为它的调用方靠返回码判断是否继续。
- **`flash.sh` 会 `rm` 传入的文件**，防止 `/tmp` 里堆着没删的升级包反复重试。
- **每次拦截都写日志**，`/data/ota-blocked.log` + syslog，方便事后确认「有没有人尝试过」。

### C.4 验证记录（2026-11-02 16:05）

四个入口逐个实测，全部拦下且无残留：

```
$ /bin/ota slient                                              → rc=0
$ /bin/ota check                                               → rc=0
$ /bin/ota upgrade http://example.com/evil.bin deadbeef 30000000 → rc=0
$ /bin/flash.sh /tmp/fake.bin 1 1                              → rc=1

/data/ota-blocked.log:
2026-11-02 16:05:46 BLOCKED [ota] args=[slient]
2026-11-02 16:05:46 BLOCKED [ota] args=[check]
2026-11-02 16:05:46 BLOCKED [ota] args=[upgrade http://example.com/evil.bin deadbeef 30000000]
2026-11-02 16:05:46 BLOCKED [flash.sh] args=[/tmp/fake.bin 1 1]
```

**持久性**：`/` 是 `ubifs rw`（不是 tmpfs），`/bin` 与 `/etc` 的改动重启后保留。
已确认 `mico_ai_crontab`（名字有误导性）**不会重写系统 crontab**——
它的 42 处 `crontab` 字符串全是自己的 AI 定时任务日志，二进制里没有任何
`/etc/crontabs` 路径。cron 服务已重启加载新表。

### C.5 ⚠️ 副作用与注意事项

1. **「检查更新」按钮会失效**（返回成功但不做事）。如果将来想手动升级，必须先恢复（C.6）。
2. **米家 App 的升级功能同样失效**，且不会报错——这是刻意的，避免触发 App 侧的重试。
3. **不影响任何其它功能**：已验证 `pns` 正常、其余 4 条 cron 任务保留
   （`easy_logcut` / `network_probe` / `pns refresh` / `check_mediaplayer_status`）。
4. **不能防住「从 U 盘/串口强刷」**。那是物理手段，本方案只防网络与云端推送。
5. 若将来厂商把 `flash.sh` 改名或换路径（OTA 自己改自己），这套拦截会失效。
   **重启后跑一次 `md5sum /bin/ota /bin/flash.sh` 对比预期值**即可发现。

### C.6 恢复方法

原件已备份在 `/data/ota-disable-backup/`：

```sh
cp /data/ota-disable-backup/_bin_ota                /bin/ota
cp /data/ota-disable-backup/_bin_flash.sh           /bin/flash.sh
cp /data/ota-disable-backup/_etc_crontabs_root      /etc/crontabs/root
cp /data/ota-disable-backup/_var_spool_cron_crontabs_root /var/spool/cron/crontabs/root
chmod 755 /bin/ota /bin/flash.sh
/etc/init.d/cron restart
```

快照 MD5（可用于校验备份是否完好）：

```
_bin_ota                          9d2f70e2d24f9c7503338e0cb94a53ba
_bin_flash.sh                     d5ef8ff9f06b4ef5e56ca4fea8ff6c54
_etc_crontabs_root                a370f5a2c869ca74edae0d792015ca51
_var_spool_cron_crontabs_root     a370f5a2c869ca74edae0d792015ca51
```

拦截后 `MD5(/bin/ota) = MD5(/bin/flash.sh) = MD5(/bin/ota-blocker.sh) = 3a7afc5f206c689118564b9c515bee83`。

---

## 附录 D：系统备份与恢复（2026-11-02 已实施）

> 完整手册见 `backup/s12-1.74.18-20261102/RESTORE.md`
> 备份脚本 `backup/backup-s12.sh`，恢复脚本 `backup/restore-s12.sh`

### D.1 一个意外的好消息：设备本来就是 A/B 双系统

翻 `/proc/mtd` 时发现 6 个分区，其中 **两个 96MB 的 rootfs 槽**：

```
mtd0: 00200000 00020000 "nandboot"    2 MiB   U-Boot + env
mtd1: 01000000 00020000 "boot0"      16 MiB   内核 A
mtd2: 01000000 00020000 "boot1"      16 MiB   内核 B
mtd3: 06000000 00020000 "system0"    96 MiB   rootfs A = 1.74.15（原厂未动）
mtd4: 06000000 00020000 "system1"    96 MiB   rootfs B = 1.74.18（当前系统）
mtd5: 01800000 00020000 "data"       24 MiB   /data
```

`system0` 里是一个**完整、干净、未被修改的 1.74.15**（只读挂载验证过，
`/bin/ota` 和 `/bin/flash.sh` 还是 2023-02-28 的原件）。

U-Boot 环境里自带槽位切换与失败回退：

```
boot_part=boot1        ← 当前从 system1 启动
boot_failcnt=0

boot_failed=if itest ${boot_failcnt} == 1; then setenv boot_failcnt 2; setenv boot_part boot1;
            else if itest ${boot_failcnt} == 2; then setenv boot_failcnt 1; setenv boot_part boot0;
            else run set_boot_flag; fi; fi;
```

**这意味着最坏情况下的兜底根本不需要电脑**：

```sh
fw_setenv boot_part boot0 && reboot     # 30 秒回到 1.74.15
```

这个发现大幅降低了后续所有 AEC 实验的风险——改坏了就切槽。

### D.2 备份了哪些东西

| 文件 | 内容 | 原始 | 压缩 |
| --- | --- | --- | --- |
| `mtd0-nandboot.rebuilt.raw.gz` | U-Boot + 环境变量 | 2 MiB | 1.4 MiB |
| `mtd1-boot0.raw.gz` | 内核 A | 16 MiB | 5.1 MiB |
| `mtd2-boot1.raw.gz` | 内核 B | 16 MiB | 5.1 MiB |
| `mtd3-system0.raw.gz` | rootfs A（1.74.15 原厂） | 96 MiB | 39 MiB |
| `mtd4-system1.raw.gz` | rootfs B（1.74.18 当前） | 96 MiB | 32 MiB |
| `mtd5-data.raw.gz` | `/data`（含 WiFi、Token、OTA 备份） | 24 MiB | 1.7 MiB |

合计 88 MiB。全部经 **设备端 md5 与本机逐字节对拍一致**（mtd1/2/3/4/5），
UBIFS 结构可解析（768 个 EC header，与 `ubinfo` 报告的 768 LEB 吻合）。

### D.3 备份过程中踩到的 4 个坑

这一段值得单列，因为每个都会导致「看起来成功、实际不可用」：

1. **`dd` 遇坏块提前 EOF**
   `dd if=/dev/mtd0ro bs=64k` 在 block 15（offset `0x0f0000`）就停了，
   只拿到 960 KiB，但**返回码是 0**，不检查尺寸就会以为备份成功。
   必须逐块读，坏块填 `0xFF` 并记录。

2. **macOS 的 `tr` 会把单字节扩成双字节**
   `dd if=/dev/zero bs=65536 | tr '\000' '\377'` 输出 **131072 字节**。
   换成 `python3 -c "import sys;sys.stdout.buffer.write(b'\xff'*65536)"` 才对。

3. **非交互 SSH 下 `PATH` 不含 busybox 目录，且静默失败**
   `ssh host 'sha256sum'` → `ash: not found`（rc=127），**stdout 为空**。
   在 `$(...)` 里就变成空字符串，跟本机哈希一比，结论是「不一致」——
   差点误判备份坏了。所有远端命令必须显式
   `export PATH=/usr/sbin:/usr/bin:/sbin:/bin`。

4. **设备根本没有 `sha256sum`**，只有 `md5sum`。校验逻辑全用 md5。

### D.4 恢复路径分三档

| 场景 | 手段 | 需要外设 | 风险 |
| --- | --- | --- | --- |
| 只改坏 rootfs / 驱动 / 服务 | `fw_setenv boot_part boot0` 切槽 | 否 | 极低 |
| 想在保留 1.74.18 前提下修复 | 从 system0 启动，`ubiformat /dev/mtd4 -f -` 流式回写 | 否 | 低 |
| U-Boot 本身坏了 | USB 烧录（Amlogic USB Burning Tool） | **是** | 高 |

第三档是唯一的盲区。而且注意：**设备上没有 `nandwrite` / `flash_erase`**，
只有 `dd`、`flash_eraseall`、`ubiformat`。用 `dd` 直接写 `mtdblock` 不擦除，
NAND 只能 1→0，会写坏。所以 **mtd0 无法靠设备自身恢复**，必须外部工具。
——不过正常操作（改 rootfs、换二进制、调 ALSA）根本碰不到 mtd0。

### D.5 对 AEC 工作的实际意义

后续要在设备上跑自建的音频端点（`/data/s12audio/`）、改 init 脚本、
可能要动 ALSA 配置甚至内核模块。现在有了：

- **秒级回退**：`fw_setenv boot_part boot0`
- **分钟级还原**：`restore-s12.sh system1`（从 system0 启动后流式回写 32MB）
- **数据无损**：`/data` 单独备份，WiFi 凭证和 Token 都在

唯一要养成的习惯：**大改动前先跑一次 `backup-s12.sh`**，
因为 1.74.18 这个槽一旦被 OTA 或误操作覆盖，只有备份能拿回来
（system0 只能给到 1.74.15）。
