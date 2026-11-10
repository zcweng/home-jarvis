/* 六麦延迟求和波束成形实现。详见 beam.h 顶部的取舍说明。
 *
 * 处理链（每 10ms 帧调用一次）：
 *   ① 逐通道推入分数延迟历史 + 分析历史
 *   ② 每 500ms：对 6 路 1024 点加窗 FFT → 频带（300-3500Hz）电平
 *      → 选参考通道 → R = A*·B 做 PHAT 加权 → IFFT → 峰值位置 = TDOA
 *   ③ 坏估计（|τ|>11 或峰值不锐或信号太弱）沿用上次，永不输出垃圾
 *   ④ 每样本：按 τ 做线性插值分数延迟，再按均衡增益求和
 *
 * 常量的来历：
 *   MIN_RATIO 2.0     —— 离线原型上，语音段峰值锐度 3~8、纯噪声段 1.2~1.8；
 *                        取 2.0 能挡住全部噪声更新（噪声上估出 τ 会真的抵消语音）。
 *   GAIN_CLIP ±6dB    —— ch1/ch5 实测比 ch0/ch3 低 4~6dB，超过 6dB 的差异
 *                        更可能是「这一路坏了」，不该靠放大硬追。
 *   EMA_TAU 0.5       —— 500ms 更新一次，0.5 的 EMA 等效 ~1s 平滑，
 *                        人走动时能跟上，静止时抖动 < 0.2 sample。
 */
#include "beam.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define BAND_LO 300.0
#define BAND_HI 3500.0
#define MIN_RATIO 2.0
#define GAIN_CLIP_DB 6.0
#define EMA_TAU 0.5
#define EMA_GAIN 0.3
#define EMA_BOOST 0.05
#define BOOST_MAX_DB 9.0
#define LEVEL_FLOOR_DB -75.0 /* 参考通道频带低于此值就不更新（纯静音段） */

/* ----------------------------------------------------------- 1024 点复数 FFT */

static int fft_rev[BEAM_ANALYSIS];
static int fft_init_done = 0;

static void fft_init(void) {
  int n = BEAM_ANALYSIS, bits = 0;
  while ((1 << bits) < n) bits++;
  for (int i = 0; i < n; i++) {
    int r = 0;
    for (int b = 0; b < bits; b++)
      if (i & (1 << b)) r |= 1 << (bits - 1 - b);
    fft_rev[i] = r;
  }
  fft_init_done = 1;
}

/* 原地 FFT（输入已按 re/im 填好，长度固定 BEAM_ANALYSIS） */
static void fft_run(double *re, double *im) {
  int n = BEAM_ANALYSIS;
  if (!fft_init_done) fft_init();
  for (int i = 0; i < n; i++) {
    int j = fft_rev[i];
    if (j > i) {
      double t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
  }
  for (int len = 2; len <= n; len <<= 1) {
    double ang = -2.0 * M_PI / len;
    double wr = cos(ang), wi = sin(ang);
    for (int i = 0; i < n; i += len) {
      double cr = 1.0, ci = 0.0;
      for (int k = 0; k < len / 2; k++) {
        int a = i + k, b = i + k + len / 2;
        double xr = re[b] * cr - im[b] * ci;
        double xi = re[b] * ci + im[b] * cr;
        re[b] = re[a] - xr;
        im[b] = im[a] - xi;
        re[a] += xr;
        im[a] += xi;
        double nr = cr * wr - ci * wi;
        ci = cr * wi + ci * wr;
        cr = nr;
      }
    }
  }
}

static void fft_ifft(double *re, double *im) {
  int n = BEAM_ANALYSIS;
  for (int i = 0; i < n; i++) im[i] = -im[i];
  fft_run(re, im);
  for (int i = 0; i < n; i++) {
    re[i] /= n;
    im[i] = -im[i] / n;
  }
}

/* --------------------------------------------------------------- 窗函数缓存 */

static double fft_win[BEAM_ANALYSIS];
static int win_done = 0;

static void win_init(void) {
  for (int i = 0; i < BEAM_ANALYSIS; i++)
    fft_win[i] = 0.5 - 0.5 * cos(2.0 * M_PI * i / (BEAM_ANALYSIS - 1));
  win_done = 1;
}

/* 频带功率 → 带内均方值。
 * 加窗 FFT 的 Parseval：Σ|X|² ≈ N·Σw²·σ² （σ² = 均方值）。
 * 这里只保留正频率半边，所以 ×2；Hann 窗 Σw² ≈ 3N/8。 */
static double band_pow(double energy, int n) {
  return 2.0 * energy / (n * (3.0 * n / 8.0));
}

/* --------------------------------------------------------------- 对外接口 */

void beam_init(beam_t *b, int n_ch, int update_ms) {
  memset(b, 0, sizeof(*b));
  if (n_ch > BEAM_MAX_CH) n_ch = BEAM_MAX_CH;
  if (n_ch < 2) n_ch = 2;
  b->n_ch = n_ch;
  b->update_every = 16000 / 1000 * (update_ms > 0 ? update_ms : 500);
  for (int c = 0; c < n_ch; c++) {
    b->gain_db[c] = 0.0;
    b->gain_lin[c] = 1.0;
    b->gain_a[c] = 1.0;
    b->gain_b[c] = 1.0;
  }
  b->fade_len = 1600; /* 100ms：比 10ms 帧长一个量级，听不出台阶 */
  b->fade_left = 0;
  b->ref = 0;
  b->boost_db = 0.0;
  if (!win_done) win_init();
  if (!fft_init_done) fft_init();
}

/* 取分析环里的时间序（最旧 → 最新）。两段 memcpy，不做逐样本取模。 */
static void an_read(const beam_t *b, int c, double *out) {
  const float *src = b->an[c];
  int n = BEAM_ANALYSIS;
  int start = b->ap; /* 最旧样本位置 */
  int first = n - start;
  for (int i = 0; i < first; i++) out[i] = src[start + i];
  for (int i = 0; i < start; i++) out[first + i] = src[i];
}

/* 每 500ms 一次：估时延 + 更新均衡增益 */
static void beam_update(beam_t *b) {
  int n = BEAM_ANALYSIS;
  int nc = b->n_ch;
  static double re[BEAM_MAX_CH][BEAM_ANALYSIS];
  static double im[BEAM_MAX_CH][BEAM_ANALYSIS];
  static double sig[BEAM_MAX_CH][BEAM_ANALYSIS];
  double energy[BEAM_MAX_CH];

  double bin_hz = (double)16000 / n;
  int k_lo = (int)ceil(BAND_LO / bin_hz);
  int k_hi = (int)floor(BAND_HI / bin_hz);
  if (k_hi >= n / 2) k_hi = n / 2 - 1;

  for (int c = 0; c < nc; c++) {
    an_read(b, c, sig[c]);
    for (int i = 0; i < n; i++) {
      re[c][i] = sig[c][i] * fft_win[i];
      im[c][i] = 0.0;
    }
    fft_run(re[c], im[c]);
    double e = 0.0;
    for (int k = k_lo; k <= k_hi; k++) e += re[c][k] * re[c][k] + im[c][k] * im[c][k];
    energy[c] = e;
  }

  /* 参考通道：频带能量最大的一路（它最可能是「离人最近」的麦克风） */
  int ref = 0;
  for (int c = 1; c < nc; c++)
    if (energy[c] > energy[ref]) ref = c;
  b->ref = ref;
  /* 频带电平：能量已含窗和 FFT 增益，这里只用来判断「有没有信号」，
     绝对值不重要，按 Parseval 折算到满量程 */
  double ref_db = 10.0 * log10(band_pow(energy[ref], n) + 1e-30);
  b->band_db = ref_db;
  if (ref_db < LEVEL_FLOOR_DB) {
    b->est_low++;
    return; /* 静音段不更新，沿用上次 τ */
  }

  /* GCC-PHAT：对每路算 R = conj(A_ref)·A_c，PHAT 加权后逆变换找峰 */
  static double phat_re[BEAM_ANALYSIS], phat_im[BEAM_ANALYSIS];
  static double new_tau[BEAM_MAX_CH];
  double worst_q = 1e9;

  for (int c = 0; c < nc; c++) {
    if (c == ref) {
      new_tau[c] = 0.0;
      continue;
    }
    for (int k = 0; k < n; k++) {
      /* (a + bi)(c + di) 取 conj(A_ref) * A_c */
      double ar = re[ref][k], ai = -im[ref][k];
      double br = re[c][k], bi = im[c][k];
      double rr = ar * br - ai * bi;
      double ri = ar * bi + ai * br;
      int in_band = (k >= k_lo && k <= k_hi);
      double mag = sqrt(rr * rr + ri * ri);
      if (in_band && mag > 1e-12) {
        rr /= mag;
        ri /= mag;
      } else if (in_band) {
        rr = 0.0;
        ri = 0.0;
      } else {
        rr = 0.0;
        ri = 0.0; /* 带外清零 = 带限，等价于离线原型的 band 掩码 */
      }
      phat_re[k] = rr;
      phat_im[k] = ri;
    }
    fft_ifft(phat_re, phat_im);

    /* 在 ±BEAM_MAX_TAU 内找峰（正 = c 落后于 ref） */
    double best = -1e30;
    int best_lag = 0;
    for (int lag = -BEAM_MAX_TAU; lag <= BEAM_MAX_TAU; lag++) {
      int idx = ((lag % n) + n) % n;
      if (phat_re[idx] > best) {
        best = phat_re[idx];
        best_lag = lag;
      }
    }
    /* 抛物线插值出小数部分 */
    int i0 = ((best_lag % n) + n) % n;
    int im1 = (i0 - 1 + n) % n, ip1 = (i0 + 1) % n;
    double y0 = phat_re[im1], y1 = phat_re[i0], y2 = phat_re[ip1];
    double den = y0 - 2 * y1 + y2;
    double frac = 0.0;
    if (fabs(den) > 1e-12) {
      frac = 0.5 * (y0 - y2) / den;
      if (frac > 1.0) frac = 1.0;
      if (frac < -1.0) frac = -1.0;
    }
    /* 峰值锐度：峰 / 峰外均值 */
    double sum = 0.0;
    int cnt = 0;
    for (int lag = -BEAM_MAX_TAU; lag <= BEAM_MAX_TAU; lag++) {
      if (abs(lag - best_lag) <= 2) continue;
      int idx = ((lag % n) + n) % n;
      sum += fabs(phat_re[idx]);
      cnt++;
    }
    double q = cnt ? best / (sum / cnt + 1e-30) : 0.0;
    if (q < worst_q) worst_q = q;
    new_tau[c] = best_lag + frac;
  }

  b->last_q = worst_q;
  if (worst_q < MIN_RATIO) {
    b->est_rej++;
    return; /* 坏估计：沿用上次 τ，不更新 */
  }

  /* 去掉公共平移：参考通道的选择不影响求和结果，避免参考换路时整体漂移 */
  double mean_tau = 0.0;
  for (int c = 0; c < nc; c++) mean_tau += new_tau[c];
  mean_tau /= nc;

  for (int c = 0; c < nc; c++) {
    double t = new_tau[c] - mean_tau;
    if (t > BEAM_MAX_TAU) t = BEAM_MAX_TAU;
    if (t < -BEAM_MAX_TAU) t = -BEAM_MAX_TAU;
    b->tau[c] = b->valid ? EMA_TAU * t + (1.0 - EMA_TAU) * b->tau[c] : t;
  }
  b->valid = 1;
  b->est_ok++;

  /* 幅度均衡：以各路频带电平的均值（dB）为目标，夹在 ±GAIN_CLIP_DB */
  double mean_db = 0.0;
  for (int c = 0; c < nc; c++) mean_db += 10.0 * log10(band_pow(energy[c], n) + 1e-30);
  mean_db /= nc;
  for (int c = 0; c < nc; c++) {
    double db = 10.0 * log10(band_pow(energy[c], n) + 1e-30);
    double g = mean_db - db;
    if (g > GAIN_CLIP_DB) g = GAIN_CLIP_DB;
    if (g < -GAIN_CLIP_DB) g = -GAIN_CLIP_DB;
    b->gain_db[c] = (1.0 - EMA_GAIN) * b->gain_db[c] + EMA_GAIN * g;
    b->gain_lin[c] = pow(10.0, b->gain_db[c] / 20.0);
  }

  /* 电平补偿：把「最响通道 − 均值」慢慢跟住，补掉相干平均相对选最响的损失 */
  double max_db = -1e9;
  for (int c = 0; c < nc; c++) {
    double db = 10.0 * log10(band_pow(energy[c], n) + 1e-30);
    if (db > max_db) max_db = db;
  }
  double boost = max_db - mean_db;
  if (boost < 0) boost = 0;
  if (boost > BOOST_MAX_DB) boost = BOOST_MAX_DB;
  /* 第一次直接取测量值，别让用户在头 30 秒里听一个慢慢涨起来的耳朵 */
  if (b->est_ok <= 1)
    b->boost_db = boost;
  else
    b->boost_db = (1.0 - EMA_BOOST) * b->boost_db + EMA_BOOST * boost;

  /* 施加的延迟 = -τ（落后就提前），再整体抬到 ≥ 0，多出的常数延迟 ≤ 22 sample */
  double dmin = 1e9;
  for (int c = 0; c < nc; c++)
    if (-b->tau[c] < dmin) dmin = -b->tau[c];
  for (int c = 0; c < nc; c++) {
    double d = -b->tau[c] - dmin;
    if (d < 0) d = 0;
    if (d > BEAM_HIST - 3) d = BEAM_HIST - 3;
    b->delay[c] = d;
  }

  /* 新参数上膛：淡化期间从 a 混到 b。已经在淡化就接着当前的 b 走（不打断）。 */
  for (int c = 0; c < nc; c++) {
    if (b->fade_left > 0) {
      /* 未走完的淡化：取当前混合值当新起点，避免跳变叠加 */
      double w = 1.0 - (double)b->fade_left / b->fade_len;
      b->delay_a[c] = b->delay_a[c] * (1 - w) + b->delay_b[c] * w;
      b->gain_a[c] = b->gain_a[c] * (1 - w) + b->gain_b[c] * w;
    } else {
      b->delay_a[c] = b->delay_b[c];
      b->gain_a[c] = b->gain_b[c];
    }
    b->delay_b[c] = b->delay[c];
    b->gain_b[c] = b->gain_lin[c];
  }
  b->fade_left = b->fade_len;
}

/* 按一组延迟/增益求一次和 */
static inline double beam_sum(const beam_t *b, const double *delay, const double *gain,
                              int nc, double gsum) {
  double acc = 0.0;
  for (int c = 0; c < nc; c++) {
    double d = delay[c];
    int i0 = (int)d;
    double fr = d - i0;
    int a = (b->hp - i0 + BEAM_HIST) % BEAM_HIST;
    int bb = (a - 1 + BEAM_HIST) % BEAM_HIST;
    double s = b->hist[c][a] * (1.0 - fr) + b->hist[c][bb] * fr;
    acc += gain[c] * s;
  }
  return acc / gsum;
}

void beam_process(beam_t *b, const int16_t *const *in, int n, int16_t *out) {
  int nc = b->n_ch;
  double gsum_a = 0.0, gsum_b = 0.0;
  for (int c = 0; c < nc; c++) {
    gsum_a += b->gain_a[c];
    gsum_b += b->gain_b[c];
  }
  if (gsum_a <= 0) gsum_a = 1.0;
  if (gsum_b <= 0) gsum_b = 1.0;
  double boost = pow(10.0, b->boost_db / 20.0);
  if (boost < 0.5) boost = 0.5;
  gsum_a /= boost;
  gsum_b /= boost;

  for (int i = 0; i < n; i++) {
    b->hp = (b->hp + 1) % BEAM_HIST;
    for (int c = 0; c < nc; c++)
      b->hist[c][b->hp] = (float)in[c][i];

    double v;
    if (b->fade_left > 0) {
      double w = 1.0 - (double)b->fade_left / b->fade_len;
      double sa = beam_sum(b, b->delay_a, b->gain_a, nc, gsum_a);
      double sb = beam_sum(b, b->delay_b, b->gain_b, nc, gsum_b);
      v = sa * (1.0 - w) + sb * w;
      b->fade_left--;
    } else {
      v = beam_sum(b, b->delay_b, b->gain_b, nc, gsum_b);
    }
    if (v > 32767.0) v = 32767.0;
    if (v < -32768.0) v = -32768.0;
    out[i] = (int16_t)lrint(v);
  }

  /* 分析历史按帧整体推入（比逐样本取模省事，也不慢） */
  if (n >= BEAM_ANALYSIS) {
    for (int c = 0; c < nc; c++)
      for (int i = 0; i < BEAM_ANALYSIS; i++)
        b->an[c][i] = (float)(in[c][n - BEAM_ANALYSIS + i] / 32768.0);
    b->ap = 0;
  } else {
    int first = BEAM_ANALYSIS - b->ap;
    int k = n < first ? n : first;
    for (int c = 0; c < nc; c++) {
      for (int i = 0; i < k; i++) b->an[c][b->ap + i] = (float)(in[c][i] / 32768.0);
      for (int i = 0; i < n - k; i++) b->an[c][i] = (float)(in[c][k + i] / 32768.0);
    }
    b->ap = (b->ap + n) % BEAM_ANALYSIS;
  }

  b->since_update += n;
  if (b->since_update >= b->update_every) {
    b->since_update = 0;
    beam_update(b);
  }
}

int beam_stats(const beam_t *b, char *buf, int cap) {
  int off = 0;
  off += snprintf(buf + off, (size_t)(cap - off), "%s ref=%d q=%.1f lvl=%.0f",
                  b->valid ? "ok" : "init", b->ref, b->last_q, b->band_db);
  off += snprintf(buf + off, (size_t)(cap - off), " tau[");
  for (int c = 0; c < b->n_ch; c++)
    off += snprintf(buf + off, (size_t)(cap - off), "%s%.1f", c ? "," : "", b->tau[c]);
  off += snprintf(buf + off, (size_t)(cap - off), "] g[");
  for (int c = 0; c < b->n_ch; c++)
    off += snprintf(buf + off, (size_t)(cap - off), "%s%.1f", c ? "," : "", b->gain_db[c]);
  off += snprintf(buf + off, (size_t)(cap - off), "] boost=%.1fdB ok=%lu rej=%lu low=%lu",
                  b->boost_db, b->est_ok, b->est_rej, b->est_low);
  return off;
}