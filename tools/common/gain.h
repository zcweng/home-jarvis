#pragma once
/* 全仓唯一的音量语义：pct / wire-byte / 线性增益 / dB 的互相换算。
 *
 * 为什么单独抽出来：审查发现同一件事有三份实现、三套 clamp 边界
 *   - jarvis/volume.py::percent_to_gain_byte   （dB 曲线，允许 0）
 *   - s12audio.c::vol_set_pct                  （线性，下限 5）
 *   - tools/s12led/volumed.c::apply_force      （线性，下限 5）
 * 并且旧 s12audio 里播放增益有**两条来源**：下行 F_VOLUME 字节与
 * /tmp/jarvis-vol.pct 文件各算一遍 g_vol。两者曲线定义不同，于是音量到底
 * 是多少取决于哪条路径最后写下 g_vol —— 这是不可推理的状态。
 *
 * 现在定为唯一定义（与 jarvis/volume.py 严格一致）：
 *
 *   pct  ∈ [5, 100]            全链路唯一的音量真值（整数）
 *   byte = 60 + round(dB)     下行 F_VOLUME 的线上表示，dB = 20*log10(pct/100)
 *   gain = pct/100            线性增益（= 10^(dB/20)，两者等价）
 *
 * 增益只在一处施加：设备端下行 PCM（s12audio）。Mac 侧的 peak_db 是
 * **TTS 响度归一化**，不是用户音量，两者不重叠，不能互相替代。
 */
#include <math.h>

#define JARVIS_VOL_MIN_PCT 5
#define JARVIS_VOL_MAX_PCT 100

/* wire byte 的定义域：0 = -60dB，60 = 0dB。 */
#define JARVIS_VOL_BYTE_ZERO_DB 60

static inline int jarvis_vol_clamp(int pct) {
  if (pct < JARVIS_VOL_MIN_PCT) return JARVIS_VOL_MIN_PCT;
  if (pct > JARVIS_VOL_MAX_PCT) return JARVIS_VOL_MAX_PCT;
  return pct;
}

/* 线性增益：5% → 0.05，100% → 1.0。 */
static inline double jarvis_pct_gain(int pct) {
  return (double)jarvis_vol_clamp(pct) / 100.0;
}

static inline double jarvis_pct_db(int pct) { return 20.0 * log10(jarvis_pct_gain(pct)); }

/* pct → 下行 F_VOLUME 字节。与 jarvis/volume.py::percent_to_gain_byte 一致。 */
static inline int jarvis_pct_to_byte(int pct) {
  double db = jarvis_pct_db(pct);
  if (db < -60.0) db = -60.0;
  if (db > 0.0) db = 0.0;
  return (int)lround(JARVIS_VOL_BYTE_ZERO_DB + db);
}

/* 下行 F_VOLUME 字节 → pct。jarvis_pct_to_byte 的逆（四舍五入到整数 pct）。 */
static inline int jarvis_byte_to_pct(int byte) {
  if (byte < 0) byte = 0;
  if (byte > JARVIS_VOL_BYTE_ZERO_DB) byte = JARVIS_VOL_BYTE_ZERO_DB;
  double gain = pow(10.0, (double)(byte - JARVIS_VOL_BYTE_ZERO_DB) / 20.0);
  return jarvis_vol_clamp((int)lround(gain * 100.0));
}

/* 灯弧：12 颗灯，四舍五入；100% 必定满环，非 0 至少亮 1 颗。
 * 与 tools/s12led/led-set.sh::paint_volume 的 n=((pct*12+50)/100) 等价。 */
static inline int jarvis_vol_led_count(int pct) {
  int p = jarvis_vol_clamp(pct);
  int n = (p * 12 + 50) / 100;
  if (n < 1) n = 1;
  if (p >= 100) n = 12;
  return n;
}

/* 提示音幅度：低音量时也要能听见，但不线性放大。 */
static inline double jarvis_sfx_amp(int pct) {
  double level = jarvis_pct_gain(pct);
  return 0.35 + 0.65 * (level * level);
}