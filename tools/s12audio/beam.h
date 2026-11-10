#pragma once
/* 六麦延迟求和波束成形（delay-and-sum + GCC-PHAT 时延估计）。
 *
 * 为什么单独一个模块：
 *   1. 不依赖 ALSA / speexdsp，可以在 Mac 上原生编译，用与离线 numpy
 *      原型**同一份录音**跑，直接比对输出（tools/beamtest）；
 *   2. 设备端只负责喂帧，算法逻辑与 IO 解耦。
 *
 * 设计约束（实测得出，见 docs/S12-AEC数据流方案.md §4.8）：
 *   · 阵列口径小：两两 |TDOA| 实测 ≤ 1.9 sample（≈41mm 路程差），
 *     所以搜索窗 ±11 sample 足够，且**超过 ±11 一律判为坏估计**；
 *   · 通道间噪声相干度在 300-800Hz 达 0.49（低频轰鸣/机箱耦合），
 *     1.6kHz 以上只有 0.07-0.11 —— 延迟求和能压的是后者；
 *   · 因此延迟求和相对「选最响一路」的 SNR 增益只有 ~1.5dB，
 *     它的主要价值是**稳定**（不再逐帧换通道，上行帧能量跳变 p95 从
 *     10dB 降到 ~3dB），以及把 6 路一起用上。
 */
#include <stdint.h>

#define BEAM_MAX_CH 6
#define BEAM_ANALYSIS 1024 /* GCC-PHAT 分析窗：64ms */
#define BEAM_MAX_TAU 11    /* |TDOA| 上限（sample），超出即判坏估计 */
#define BEAM_HIST 64       /* 分数延迟历史长度，需 > 2*BEAM_MAX_TAU */

typedef struct {
  int n_ch;
  int update_every; /* 每多少样本更新一次时延，8000 = 500ms */

  /* 分数延迟历史（逐通道） */
  float hist[BEAM_MAX_CH][BEAM_HIST];
  int hp;

  /* GCC-PHAT 分析历史（逐通道） */
  float an[BEAM_MAX_CH][BEAM_ANALYSIS];
  int ap;
  int since_update;

  /* 当前状态 */
  double tau[BEAM_MAX_CH];      /* 相对参考通道的到达时延（sample），正 = 落后 */
  double delay[BEAM_MAX_CH];    /* 实际施加的延迟（sample），已整体抬成 ≥ 0 */
  double gain_db[BEAM_MAX_CH];  /* 幅度均衡（dB） */
  double gain_lin[BEAM_MAX_CH];
  int ref;
  int valid; /* 是否拿到过可用估计 */

  /* 参数切换要交叉淡化：延迟差 0.3 sample 在 2kHz 就是 ~40° 相位差，
     直接换会让求和幅度瞬变几 dB —— 实测 BEAM 的帧能量跳变 p95 反而
     从 7dB 涨到 10dB，就是每 500ms 换参数换出来的。 */
  double delay_a[BEAM_MAX_CH], gain_a[BEAM_MAX_CH]; /* 旧参数（淡化起点） */
  double delay_b[BEAM_MAX_CH], gain_b[BEAM_MAX_CH]; /* 新参数（淡化终点） */
  int fade_left, fade_len;

  /* 电平补偿：6 路相干平均的自然电平是「各路均值」，而旧实现是「选最响一路」，
     实测平均比最响低 3~4dB。不补回来就等于让耳朵更聋，所以把
     (最响 − 均值) 用一个很慢的 EMA 跟住，乘到输出上。
     α 取 0.02（≈12s）是刻意的：它代表的是「阵列/房间」的固定差，
     不该跟着语音起伏走，否则就是 AGC，会抽气也会把静音噪声抬起来。 */
  double boost_db;

  /* 诊断 */
  unsigned long est_ok, est_rej, est_low;
  double last_q;    /* 上次 TDOA 峰值锐度（峰值/均值），坏估计会偏低 */
  double band_db;   /* 参考通道分析窗内的频带电平 dBFS */
} beam_t;

void beam_init(beam_t *b, int n_ch, int update_ms);
void beam_process(beam_t *b, const int16_t *const *in, int n, int16_t *out);
/* 把当前状态写成人读的一行（塞进 STATS），返回写入长度 */
int beam_stats(const beam_t *b, char *buf, int cap);