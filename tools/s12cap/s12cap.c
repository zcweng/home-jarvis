// ===========================================================================
// s12cap —— S12 六麦阵列原始多通道采集器（测量 / 标定专用）
//
// 与 s12audio 的区别只有一件事：**不上行、不选路、不做 AEC**，
// 把 8 个 ALSA 通道原样（去直流 + 固定预放大，与 s12audio 采集端完全同参）
// 写成 8ch WAV，用于离线分析阵列几何（成对 TDOA / 相干性 / 极性 / 增益差）。
//
// 为什么要单独一个工具而不是给 s12audio 加 dump 开关：
//   · 波束成形的**第一性问题**是「这个阵列到底能不能用」——6 路是不是同一时刻
//     采的、口径多大、有没有固定偏移。这些必须拿裸数据回答，不能在产品路径里猜。
//   · 采集独占 hw:0,0，与 s12audio 天然互斥；做成独立小二进制，跑完退出，
//     不会给守护进程留下任何「调试分支」的债。
//
// 用法（设备上）：
//   /etc/init.d/jarvis-audio stop          # 交还原厂 → pns 会占住 hw:0,0
//   /etc/init.d/pns stop                   # 再次释放
//   /data/s12cap -t 8 -o /tmp/cap.wav
//   /etc/init.d/jarvis-audio start
//
// 想直接看到成对时延，用 -d 打开设备端 GCC-PHAT 估计（不写 WAV 也能跑）。
// ===========================================================================

#define _GNU_SOURCE
#include <alsa/asoundlib.h>

#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define CAP_CH 8
#define MIC_CH 6
#define REF_CH 7

#define SR 16000
#define FRAME_MS 10
#define FRAME_SAMPLES (SR / 1000 * FRAME_MS) /* 160 */
#define FRAME_SAMPLES_TOTAL (FRAME_SAMPLES * CAP_CH)

/* 与 s12audio.c 保持一致：ADC 直流约 -5.5%FS，用极慢一阶 IIR 跟踪扣除 */
#define DC_TRACK_COEF 0.0005

/* GCC-PHAT 分析窗：512 点 @16k = 32ms，够覆盖语音的浊音段 */
#define GCC_N 512
#define GCC_MAXLAG 64 /* ±64 样本 = ±4ms，远大于 6 麦口径几何上限 */

static volatile sig_atomic_t g_stop = 0;
static int g_verbose = 1;
static double g_gain = 20.0;

static uint64_t g_clip = 0;
static uint64_t g_frames = 0;
static snd_pcm_t *g_pcm_play = NULL;
static volatile int g_play_stop = 0;

static void logf_(const char *fmt, ...) {
  if (!g_verbose) return;
  va_list ap;
  va_start(ap, fmt);
  fprintf(stderr, "[s12cap] ");
  vfprintf(stderr, fmt, ap);
  fprintf(stderr, "\n");
  va_end(ap);
  fflush(stderr);
}

static void on_signal(int s) {
  (void)s;
  g_stop = 1;
}

// --------------------------------------------------------------------------- WAV

static void put32(FILE *f, uint32_t v) {
  fputc((int)(v & 0xff), f);
  fputc((int)((v >> 8) & 0xff), f);
  fputc((int)((v >> 16) & 0xff), f);
  fputc((int)((v >> 24) & 0xff), f);
}
static void put16(FILE *f, uint16_t v) {
  fputc((int)(v & 0xff), f);
  fputc((int)((v >> 8) & 0xff), f);
}

/* data 长度未知（边采边写），先写占位，结束时回填。 */
static void wav_header(FILE *f, int ch, int rate) {
  fwrite("RIFF", 1, 4, f);
  put32(f, 0); /* 占位：文件长度 - 8 */
  fwrite("WAVEfmt ", 1, 8, f);
  put32(f, 16);
  put16(f, 1); /* PCM */
  put16(f, (uint16_t)ch);
  put32(f, (uint32_t)rate);
  put32(f, (uint32_t)(rate * ch * 2));
  put16(f, (uint16_t)(ch * 2));
  put16(f, 16);
  fwrite("data", 1, 4, f);
  put32(f, 0); /* 占位：数据长度 */
}

static void wav_patch(FILE *f, long data_bytes) {
  long total = data_bytes + 36;
  fseek(f, 4, SEEK_SET);
  put32(f, (uint32_t)total);
  fseek(f, 40, SEEK_SET);
  put32(f, (uint32_t)data_bytes);
  fflush(f);
}

// --------------------------------------------------------------------------- GCC-PHAT

/* 浮点 FFT（诊断路径，1Hz 跑一次；4×A53 上 512 点 ×6 路不到 1ms）。
 * 不用定点：512 点累加必然溢出 int16，溢出会把 PHAT 峰值糊成随机值。 */

typedef struct {
  double re, im;
} cpx;

static void fft_radix2(cpx *a, int n, int inverse) {
  for (int i = 1, j = 0; i < n; i++) {
    int bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      cpx t = a[i];
      a[i] = a[j];
      a[j] = t;
    }
  }
  for (int len = 2; len <= n; len <<= 1) {
    int half = len >> 1;
    double ang = (inverse ? 2.0 : -2.0) * M_PI / len;
    for (int i = 0; i < n; i += len) {
      for (int k = 0; k < half; k++) {
        double wr = cos(ang * k), wi = sin(ang * k);
        cpx u = a[i + k];
        cpx v = a[i + half + k];
        cpx t = {v.re * wr - v.im * wi, v.re * wi + v.im * wr};
        a[i + k].re = u.re + t.re;
        a[i + k].im = u.im + t.im;
        a[i + half + k].re = u.re - t.re;
        a[i + half + k].im = u.im - t.im;
      }
    }
  }
}

/* 返回互相关峰值处的子样本 TDOA（正 = b 落后于 a），peak_ratio = 主峰/旁峰均值。 */
static double gcc_phat(const int16_t *a, const int16_t *b, int n, double *peak_ratio) {
  static cpx A[GCC_N], B[GCC_N];
  static double cc[2 * GCC_MAXLAG + 1];

  for (int i = 0; i < n; i++) {
    double w = 0.5 - 0.5 * cos(2.0 * M_PI * i / (n - 1)); /* Hann */
    A[i].re = a[i] * w;
    A[i].im = 0;
    B[i].re = b[i] * w;
    B[i].im = 0;
  }
  fft_radix2(A, n, 0);
  fft_radix2(B, n, 0);

  /* 互谱 + PHAT 加权。限带 300~3500Hz：语音主能量区，且 6 麦小口径在该频段
   * 没有空间混叠（相邻 4cm 的混叠频率约 4.3kHz）。 */
  for (int k = 0; k < n; k++) {
    double f = (double)k * SR / n;
    double re = A[k].re * B[k].re + A[k].im * B[k].im;
    double im = A[k].im * B[k].re - A[k].re * B[k].im;
    double mag = sqrt(re * re + im * im);
    double bin = (f >= 300.0 && f <= 3500.0 && mag > 1e-9) ? 1.0 / mag : 0.0;
    A[k].re = re * bin;
    A[k].im = -im * bin; /* 取共轭后做正变换 = 逆变换 */
  }
  fft_radix2(A, n, 1);

  int best = 0;
  double bv = -1e300;
  for (int lag = -GCC_MAXLAG; lag <= GCC_MAXLAG; lag++) {
    int idx = lag < 0 ? lag + n : lag;
    double v = A[idx].re;
    cc[lag + GCC_MAXLAG] = v;
    if (v > bv) {
      bv = v;
      best = lag;
    }
  }
  double frac = 0.0;
  if (best > -GCC_MAXLAG && best < GCC_MAXLAG) {
    double y0 = cc[best + GCC_MAXLAG - 1];
    double y1 = cc[best + GCC_MAXLAG];
    double y2 = cc[best + GCC_MAXLAG + 1];
    double den = (y0 - 2 * y1 + y2);
    if (fabs(den) > 1e-12) frac = 0.5 * (y0 - y2) / den;
    if (frac > 1) frac = 1;
    if (frac < -1) frac = -1;
  }
  if (peak_ratio) {
    double sum = 0;
    int cnt = 0;
    for (int lag = -GCC_MAXLAG; lag <= GCC_MAXLAG; lag++) {
      if (abs(lag - best) <= 3) continue;
      sum += fabs(cc[lag + GCC_MAXLAG]);
      cnt++;
    }
    *peak_ratio = cnt ? fabs(bv) / (sum / cnt + 1e-12) : 0.0;
  }
  return best + frac;
}

// --------------------------------------------------------------------------- main

/* 必须同时打开播放流，否则采集跑得比实时快 1.5 倍（实测本机 10s 音频只要 6.6s）。
 *
 * 原因：SoC 上的 ADC 与 DAC 共用同一个 I2S/MCLK 时钟域。只开 capture 时驱动
 * 让 ADC 自由跑，ALSA 仍按 16k 记账，于是「16k 的帧」实际是更快采样出来的
 * —— 内容被时间压缩 1.5 倍（语音音调变高），TDOA 的样本数也跟着放大 1.5 倍。
 * s12audio 没这个问题，因为它一直开着播放流。
 *
 * 所以这里写的是**纯静音**，只为把时钟锁住；ch7 环回因此恒为 0（对阵列测量无影响）。
 */
static int open_playback_silent(void) {
  int rc = snd_pcm_open(&g_pcm_play, "hw:0,0", SND_PCM_STREAM_PLAYBACK, 0);
  if (rc < 0) {
    fprintf(stderr, "打开播放失败: %s\n", snd_strerror(rc));
    return -1;
  }
  snd_pcm_hw_params_t *hw;
  snd_pcm_hw_params_alloca(&hw);
  snd_pcm_hw_params_any(g_pcm_play, hw);
  snd_pcm_hw_params_set_access(g_pcm_play, hw, SND_PCM_ACCESS_RW_INTERLEAVED);
  snd_pcm_hw_params_set_format(g_pcm_play, hw, SND_PCM_FORMAT_S16_LE);
  unsigned ch = 2;
  snd_pcm_hw_params_set_channels_near(g_pcm_play, hw, &ch);
  unsigned int r = SR;
  int dir = 0;
  snd_pcm_hw_params_set_rate_near(g_pcm_play, hw, &r, &dir);
  snd_pcm_uframes_t period = FRAME_SAMPLES;
  snd_pcm_uframes_t buffer = FRAME_SAMPLES * 8;
  snd_pcm_hw_params_set_period_size_near(g_pcm_play, hw, &period, &dir);
  snd_pcm_hw_params_set_buffer_size_near(g_pcm_play, hw, &buffer);
  rc = snd_pcm_hw_params(g_pcm_play, hw);
  if (rc < 0) {
    fprintf(stderr, "播放参数失败: %s\n", snd_strerror(rc));
    return -1;
  }
  snd_pcm_prepare(g_pcm_play);
  if (snd_pcm_start(g_pcm_play) < 0) {
    fprintf(stderr, "播放启动失败\n");
    return -1;
  }
  logf_("播放流已打开（静音，仅用于锁定时钟）ch=%u rate=%u", ch, r);
  return 0;
}

static void *silence_thread(void *arg) {
  (void)arg;
  static int16_t zero[FRAME_SAMPLES * 2];
  while (!g_play_stop && !g_stop) {
    snd_pcm_sframes_t n = snd_pcm_writei(g_pcm_play, zero, FRAME_SAMPLES);
    if (n == -EPIPE) {
      snd_pcm_prepare(g_pcm_play);
      snd_pcm_start(g_pcm_play);
      continue;
    }
    if (n < 0) {
      snd_pcm_recover(g_pcm_play, (int)n, 1);
      usleep(2000);
    }
  }
  return NULL;
}

static snd_pcm_t *open_capture(void) {
  snd_pcm_t *pcm = NULL;
  int rc = snd_pcm_open(&pcm, "hw:0,0", SND_PCM_STREAM_CAPTURE, 0);
  if (rc < 0) {
    fprintf(stderr, "打开 hw:0,0 失败: %s\n", snd_strerror(rc));
    return NULL;
  }
  snd_pcm_hw_params_t *hw;
  snd_pcm_hw_params_alloca(&hw);
  snd_pcm_hw_params_any(pcm, hw);
  snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED);
  snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_S32_LE);
  snd_pcm_hw_params_set_channels(pcm, hw, CAP_CH);
  unsigned int r = SR;
  int dir = 0;
  snd_pcm_hw_params_set_rate_near(pcm, hw, &r, &dir);
  if (r != SR) {
    fprintf(stderr, "采样率回填 %u != %d，拒绝继续（时间戳会错位）\n", r, SR);
    snd_pcm_close(pcm);
    return NULL;
  }
  snd_pcm_uframes_t period = FRAME_SAMPLES;
  snd_pcm_uframes_t buffer = FRAME_SAMPLES * 8;
  snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, &dir);
  snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer);
  rc = snd_pcm_hw_params(pcm, hw);
  if (rc < 0) {
    fprintf(stderr, "采集参数失败: %s\n", snd_strerror(rc));
    snd_pcm_close(pcm);
    return NULL;
  }
  snd_pcm_prepare(pcm);
  snd_pcm_start(pcm);
  logf_("采集就绪 %dch S32@%dHz period=%lu buffer=%lu", CAP_CH, SR,
        (unsigned long)period, (unsigned long)buffer);
  return pcm;
}

int main(int argc, char **argv) {
  double seconds = 10.0;
  const char *out = "/tmp/s12cap.wav";
  int do_gcc = 0;
  int opt;
  while ((opt = getopt(argc, argv, "t:o:g:dqh")) != -1) {
    switch (opt) {
      case 't': seconds = atof(optarg); break;
      case 'o': out = optarg; break;
      case 'g': g_gain = atof(optarg); break;
      case 'd': do_gcc = 1; break;
      case 'q': g_verbose = 0; break;
      case 'h':
        fprintf(stderr,
                "用法: %s [-t 秒] [-o wav路径] [-g 麦克风增益] [-d 设备端TDOA诊断] [-q]\n"
                "  -t  录制秒数（默认 10）\n"
                "  -o  输出 8ch WAV 路径（默认 /tmp/s12cap.wav）\n"
                "  -g  与 s12audio 一致的麦克风预放大（默认 20）\n"
                "  -d  每秒打印一次成对 GCC-PHAT 时延（诊断阵列几何）\n",
                argv[0]);
        return 0;
      default: return 1;
    }
  }
  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);

  if (!getenv("S12CAP_NO_PLAY")) {
    if (open_playback_silent() < 0) {
      fprintf(stderr, "⚠️ 无播放流时采集会快 1.5 倍，本次测量数据不可用于时延分析\n");
    } else {
      pthread_t tp;
      pthread_create(&tp, NULL, silence_thread, NULL);
      pthread_detach(tp);
    }
  }

  snd_pcm_t *pcm = open_capture();
  if (!pcm) return 1;

  FILE *f = fopen(out, "wb");
  if (!f) {
    fprintf(stderr, "无法写 %s: %s\n", out, strerror(errno));
    snd_pcm_close(pcm);
    return 1;
  }
  wav_header(f, CAP_CH, SR);

  static int32_t raw[FRAME_SAMPLES_TOTAL];
  static int16_t buf[FRAME_SAMPLES_TOTAL];
  static int16_t hist[MIC_CH][GCC_N];
  static double dc[MIC_CH] = {0};
  int dc_init = 0;
  int hist_pos = 0;

  double sum_sq[CAP_CH] = {0};
  long nstat = 0;
  int sec_done = 0;
  long frames_target = (long)(seconds * 1000.0 / FRAME_MS);
  struct timespec t0, tn;
  clock_gettime(CLOCK_MONOTONIC, &t0);

  while (!g_stop && g_frames < (uint64_t)frames_target) {
    int got = 0;
    while (got < FRAME_SAMPLES && !g_stop) {
      int want = FRAME_SAMPLES - got;
      snd_pcm_sframes_t n = snd_pcm_readi(pcm, raw + got * CAP_CH, want);
      if (n == -EPIPE) {
        snd_pcm_prepare(pcm);
        snd_pcm_start(pcm);
        got = 0;
        continue;
      }
      if (n < 0) {
        snd_pcm_recover(pcm, (int)n, 1);
        usleep(2000);
        continue;
      }
      if (n == 0) continue;
      if ((int)n > want) n = want;
      got += (int)n;
    }
    if (got < FRAME_SAMPLES) break;

    for (int i = 0; i < FRAME_SAMPLES; i++) {
      for (int c = 0; c < CAP_CH; c++) {
        int idx = i * CAP_CH + c;
        if (c < MIC_CH) {
          double v = raw[idx];
          if (dc_init) {
            dc[c] += (v - dc[c]) * DC_TRACK_COEF;
          } else {
            dc[c] += v / FRAME_SAMPLES;
          }
          double s = (v - dc[c]) / 65536.0 * g_gain;
          if (s > 32767.0) {
            s = 32767.0;
            g_clip++;
          }
          if (s < -32768.0) {
            s = -32768.0;
            g_clip++;
          }
          buf[idx] = (int16_t)s;
        } else {
          buf[idx] = (int16_t)(raw[idx] >> 16);
        }
      }
    }
    if (!dc_init) {
      logf_("DC 初值: %.0f %.0f %.0f %.0f %.0f %.0f", dc[0], dc[1], dc[2], dc[3], dc[4],
            dc[5]);
      dc_init = 1;
    }

    fwrite(buf, sizeof(int16_t), FRAME_SAMPLES_TOTAL, f);
    g_frames++;

    /* GCC 诊断用的历史环形缓冲（只留 6 路麦） */
    if (do_gcc) {
      for (int i = 0; i < FRAME_SAMPLES; i++) {
        for (int c = 0; c < MIC_CH; c++) {
          hist[c][hist_pos] = buf[i * CAP_CH + c];
        }
        hist_pos = (hist_pos + 1) % GCC_N;
      }
    }

    for (int i = 0; i < FRAME_SAMPLES; i++) {
      for (int c = 0; c < CAP_CH; c++) {
        if (c >= MIC_CH) continue;
        double v = buf[i * CAP_CH + c] / 32768.0;
        sum_sq[c] += v * v;
      }
    }
    nstat += FRAME_SAMPLES;

    if (nstat >= SR) {
      sec_done++;
      double rms[MIC_CH];
      fprintf(stderr, "[s12cap] t=%ds", sec_done);
      for (int c = 0; c < MIC_CH; c++) {
        rms[c] = sqrt(sum_sq[c] / nstat);
        fprintf(stderr, " ch%d=%.1f", c,
                rms[c] <= 1e-9 ? -120.0 : 20 * log10(rms[c]));
        sum_sq[c] = 0;
      }
      fprintf(stderr, " dBFS clip=%llu\n", (unsigned long long)g_clip);
      fflush(stderr);
      nstat = 0;

      if (do_gcc) {
        /* 从环形缓冲还原成时序（最老 → 最新） */
        static int16_t seq[MIC_CH][GCC_N];
        for (int c = 0; c < MIC_CH; c++) {
          for (int i = 0; i < GCC_N; i++) {
            seq[c][i] = hist[c][(hist_pos + i) % GCC_N];
          }
        }
        /* 主通道取当前能量最大的一路，其余对它算 TDOA */
        int ref = 0;
        double bv = -1;
        for (int c = 0; c < MIC_CH; c++) {
          if (rms[c] > bv) {
            bv = rms[c];
            ref = c;
          }
        }
        fprintf(stderr, "[s12cap]   TDOA(ref=ch%d, 正=该路落后):", ref);
        for (int c = 0; c < MIC_CH; c++) {
          if (c == ref) continue;
          double ratio = 0;
          double d = gcc_phat(seq[ref], seq[c], GCC_N, &ratio);
          fprintf(stderr, " ch%d:%+.2f(r%.1f)", c, d, ratio);
        }
        fprintf(stderr, " samples\n");
        fflush(stderr);
      }
    }
  }

  clock_gettime(CLOCK_MONOTONIC, &tn);
  double wall = (tn.tv_sec - t0.tv_sec) + (tn.tv_nsec - t0.tv_nsec) / 1e9;
  long data_bytes = (long)g_frames * FRAME_SAMPLES_TOTAL * 2;
  wav_patch(f, data_bytes);
  fclose(f);
  snd_pcm_close(pcm);
  g_play_stop = 1;
  if (g_pcm_play) {
    usleep(30000);
    snd_pcm_drop(g_pcm_play);
    snd_pcm_close(g_pcm_play);
  }

  logf_("完成 %llu 帧 (%.2fs wall) → %s  data=%ld B  clip=%llu", (unsigned long long)g_frames,
        wall, out, data_bytes, (unsigned long long)g_clip);
  return 0;
}