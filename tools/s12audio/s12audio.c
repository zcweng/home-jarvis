// ===========================================================================
// s12audio —— S12 音箱音频端点守护进程
//
// 上行（设备→Mac，8765 TCP，裸流）：8ch S32@16k 采集 → 拆通道 → AEC → 单声道 S16@16k
// 下行（Mac→设备，8766 TCP，framed）：PCM/FLUSH/VOLUME → 抖动缓冲 → 2ch S16@16k 播放
//
// 设计依据见 docs/S12-AEC数据流方案.md。三条实测事实决定了实现：
//   1. ch7 是播放 PCM(index1) 的**逐样本数字拷贝**（16k 下 corr=1.000000, max|err|=0）
//      → AEC 参考信号无需重采样对齐，直接当 ref 用
//   2. 硬件只播 index 1（右声道）→ 播放必须把 mono 复制到左右两声道
//   3. 采集只能 8ch（-c6/-c1 报 Channels count non available）
//
// 2026-11-04 设备实测（pns 停掉后 /data/probe_rate、/data/probe_stream）：
//   · hw:0,0 采集与播放都接受 16000 精确赋值（set_rate rc=0 → 16000），
//     **16k 直采成立**，不需要 48k→16k 重采样。所以下面用 set_rate 而非
//     set_rate_near，并对回填值做硬断言 —— 一旦哪天固件变了会立刻报错退出，
//     而不是带着 3 倍错位的时间戳继续跑。
//   · 8ch S32 @16k 的 period_size=160 合法，但 buffer 被 ALSA 钳到 128 帧
//     （8ms）；128 不是 160 的整数倍 → readi 会返回小于一帧的短读。
//     旧实现把 got 累加到 160 就当作整帧，于是每帧悄悄混进了上一帧的尾部，
//     留下周期性不连续 —— 这就是 AEC 永远收敛不到 20dB 的第二个原因。
//     现在采集端一律按「攒满 160 帧」处理，不会再错。
//   · ch0-5 直流约 -5%FS、语音 RMS 约 -54dBFS；ch6 恒 0；ch7 空闲时精确 0。
//
// 重要：设备上 libspeexdsp 1.2-rc1 是**定点**版，echo/preprocess 全部用 spx_int16_t。
// 所以内部处理链一律 S16，不转 float —— 既符合库接口，又比 float 省 CPU。
//
// 线程模型与**资源所有权**（每个可变状态只有一个线程写，这是并发正确性的地基）：
//   capture  → cap_ring(8ch S16) → process(解交织+DC+AEC+NS+选路) → up_ring → uplink
//   downlink → play_ring(mono S16) → playback → ALSA
//
//   g_pcm_cap   只被 capture 线程使用
//   g_pcm_play  只被 playback 线程使用（downlink 通过 g_flush_req 请求，不再直接碰句柄）
//   g_vol_pct   只被 playback 线程写（它也是 /tmp/jarvis-vol.pct 的唯一写者）
//   g_mic_muted 只被 playback 线程轮询后写；uplink 只读
//   g_mic_mute_seen 只被 playback 线程读写（旧实现被两个线程同时调用，见 2026-11-04 审查）
//
// 下行连接（P0）：同一时刻只服务一条 TCP。新连接抢占旧 session（关旧 fd + 清
// play_ring + FLUSH）；TCP keepalive + 30s 应用空闲超时踢死半开/占坑连接。
// ===========================================================================

#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <speex/speex_echo.h>
#include <speex/speex_preprocess.h>

#include <errno.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "gain.h"
#include "proto.h"
#include "ring.h"
#include "state_file.h"

#include "mute_sfx_pcm.h"
#include "vol_dong_pcm.h"

// --------------------------------------------------------------------------- 常量

#define CAP_CH 8
#define MIC_CH 6
#define REF_CH 7

#define SR 16000
#define FRAME_MS 10
#define FRAME_SAMPLES (SR / 1000 * FRAME_MS) /* 160 */
#define FRAME_BYTES_S16 (FRAME_SAMPLES * 2)  /* 320 */

#define AEC_FILTER_LEN 3200 /* 200ms @16k，speex 要求是 16 的倍数 */

/* DC 阻断：一阶 IIR，跟随 ~2Hz 直流/温漂，不碰语音低频。
 * 断点 80Hz 的二阶巴特沃斯在文档 §4.3 里写过，但实测直流是**常量**偏置
 * （-5%FS 且 1s 内几乎不变），一阶足够且不会在 80Hz 处引入相位旋转。 */
#define DC_TRACK_COEF 0.0005

#define HPF_CUTOFF_HZ 80.0 /* 仅保留给文档对齐，见上 */

#define UP_PORT 8765
#define DOWN_PORT 8766

#define PLAY_PREBUF 3       /* 起播前攒 30ms */
#define PLAY_RING_FRAMES 300 /* 3s@10ms：抗住客户端短时突发，避免挤掉开头 */
#define UP_RING_FRAMES 100   /* 1s：Mac 侧偶发卡顿不再静默丢帧（旧值 16 帧=160ms） */

/* 参考延迟：**0**。
 * 2026-11-04 设备实测（/data/bench_aec，自放自录，逐通道固定选路）：
 *   ref 延迟   0样本/32/80/160    →  新AEC ERLE 平均 5.2 / 5.1 / 0.5 / 0.3 dB
 * ch7 与麦克风在同一个 ALSA 帧里，mic 听到的就是**本帧**写入 DAC 的样本，
 * 所以同帧喂 ref 才是对的。加 5ms 以上延迟立刻崩到噪声级（0.2~0.5dB）。
 * 结论：不要引入参考延迟线。 */

#define VOL_MIN_PCT JARVIS_VOL_MIN_PCT

// --------------------------------------------------------------------------- 全局

static volatile sig_atomic_t g_stop = 0;
static int g_verbose = 1;

/* 2026-11-02 重标定：噪声底 ≈ -84 dBFS，正常说话峰值 ≈ -53 dBFS（ch3 最佳）。
 *
 * 2026-11-04 设备实测（/data/bench_aec_g，自放自录逐通道 ERLE）证明**固定数字增益
 * 是 AEC 成败的一号因素**，因为 Speex 定点版一旦饱和就冻结自适应：
 *
 *   增益    mic 电平      6-state AEC 平均 ERLE
 *   56     -7~-12 dBFS   **2.3 dB**   ← 削顶，AEC 基本失效
 *   32     -15~-20 dBFS    4.8 dB
 *   20     -21~-25 dBFS    8.6 dB
 *   14     -27~-31 dBFS   11.1 dB
 *   6      -44~-50 dBFS   11.1 dB     ← 不再提升，说明 11dB 是算法/扬声器上限
 *
 * 所以默认取 20：避开饱和，同时保留足够电平给 ASR（-25dBFS 对 16bit 完全够用）。
 * 如果 ASR 需要更响，**用 -p（后置增益）在 AEC 之后补**，不要调这个值。
 *
 * 饱和阈值不在 32767，而在「预加重之后」。mdf.c 对每个样本先做
 *   tmp32 = x[n] - 0.9*x[n-1]        // preemph = QCONST16(.9,15)
 * 再判 tmp32 是否越界，所以高频分量最多被放大 ~1.9 倍 —— 原始峰值到 17k LSB
 * 附近就可能触发。近端饱和冻结 1 帧（10ms），**参考饱和冻结 M+1 帧**；
 * 本项目 filter_len=3200 / frame=160 → M=21，即一次 ref 裁剪要停学 220ms。
 * 这就是 ch7 必须单位增益、不加任何滤波的原因。 */
static double g_gain_mic = 20.0;
static int g_select_mode = 0; /* 0=自适应 1=固定 2=平均 */
static int g_fixed_ch = 0;
static int g_aec_on = 1;
static int g_ns_on = 1;
static int g_agc_on = 0;
static int g_gain_post_db = 0;
static volatile int g_mic_muted = 0;

static unsigned long g_frames, g_upsent, g_upsent_drop, g_downframes, g_flushes;
static unsigned long g_cap_frames;
static volatile unsigned long g_hb_proc, g_hb_up, g_hb_play, g_hb_cap;
static volatile sig_atomic_t g_stats_req = 0;
static volatile sig_atomic_t g_probe_req = 0;
static unsigned long g_clip = 0;
static double g_erle = 0, g_erle_sum = 0;
static unsigned long g_erle_n = 0;
static int g_sel_ch = 0;
static int g_rate_ok = 1;

// --------------------------------------------------------------------------- 日志 / 信号

static void logf_(const char *fmt, ...) {
  if (!g_verbose) return;
  struct timeval tv;
  gettimeofday(&tv, NULL);
  struct tm tm;
  localtime_r(&tv.tv_sec, &tm);
  fprintf(stderr, "[%02d:%02d:%02d.%03d] ", tm.tm_hour, tm.tm_min, tm.tm_sec,
          (int)(tv.tv_usec / 1000));
  va_list ap;
  va_start(ap, fmt);
  vfprintf(stderr, fmt, ap);
  va_end(ap);
  fputc('\n', stderr);
}

/* 信号处理函数里只置标志 / write()。
 * 旧实现在 SIGUSR1 里直接 fprintf + localtime_r（stdio 锁 + tz 锁），
 * 一旦信号落在主线程持锁期间就会自死锁，现象是「发 SIGUSR1 就卡住」。
 * 现在 SIGUSR1 写一个字节到自管道，main 的事件循环负责真正打印。 */
static int g_sig_pipe[2] = {-1, -1};

static void on_signal(int s) {
  (void)s;
  g_stop = 1;
}

static void on_usr1(int s) {
  (void)s;
  char c = 'S';
  if (g_sig_pipe[1] >= 0) {
    ssize_t n = write(g_sig_pipe[1], &c, 1);
    (void)n;
  }
}

static void on_usr2(int s) {
  (void)s;
  char c = 'P';
  if (g_sig_pipe[1] >= 0) {
    ssize_t n = write(g_sig_pipe[1], &c, 1);
    (void)n;
  }
}

// --------------------------------------------------------------------------- 电平

static double dbfs_s16(const int16_t *x, int n) {
  if (n <= 0) return -120.0;
  double s = 0;
  for (int i = 0; i < n; i++) {
    double v = x[i] / 32768.0;
    s += v * v;
  }
  double r = sqrt(s / n);
  return r <= 1e-9 ? -120.0 : 20.0 * log10(r);
}

// 各通道原始电平诊断端口（采集线程把最新一帧的原始 S32 拷到 g_probe_snap，
// 主线程只读快照，避免和采集线程争用 ALSA 设备）
static volatile int g_probe_ready = 0;
static int32_t g_probe_snap[FRAME_SAMPLES * CAP_CH];

static void probe_channels(void) {
  if (!g_probe_ready) {
    logf_("PROBE 还没有采集快照");
    return;
  }
  double sum[CAP_CH] = {0}, sumsq[CAP_CH] = {0};
  int n = FRAME_SAMPLES;
  for (int i = 0; i < n; i++) {
    for (int c = 0; c < CAP_CH; c++) {
      double v = g_probe_snap[i * CAP_CH + c] / 2147483648.0;
      sum[c] += v;
      sumsq[c] += v * v;
    }
  }
  logf_("PROBE %d 帧：去直流后 rms(%%FS) / DC", n);
  for (int c = 0; c < CAP_CH; c++) {
    double mean = sum[c] / n;
    double var = sumsq[c] / n - mean * mean;
    if (var < 0) var = 0;
    double rms = sqrt(var);
    double db = rms <= 1e-9 ? -120.0 : 20 * log10(rms);
    logf_("  ch%d dc=%+7.3f%%  rms=%9.5f%%  %6.1f dBFS", c, mean * 100, rms * 100, db);
  }
}

// --------------------------------------------------------------------------- 状态

static snd_pcm_t *g_pcm_cap = NULL;
static snd_pcm_t *g_pcm_play = NULL;

static ring_t g_cap_ring;  /* 8ch 交织 S16 */
static ring_t g_up_ring;   /* mono S16 待上行 */
static ring_t g_play_ring; /* mono S16 待播放 */

static SpeexEchoState *g_echo = NULL;          /* g_select_mode==2 时的单通道 state */
static SpeexEchoState *g_echo_mic[MIC_CH];     /* 每路麦克风一个独立 state */
static SpeexPreprocessState *g_pp = NULL;

/* playback 线程写的状态；其它线程只读 */
static int g_vol_pct = -1; /* -1=尚未同步。**唯一的音量真值** */
static double g_vol = 1.0; /* 由 g_vol_pct 推导，不独立赋值 */
static volatile int g_pending_vol_byte = -1; /* downlink 写、playback 消费 */
static volatile int g_flush_req = 0; /* downlink 置位，playback 执行 */

// --------------------------------------------------------------------------- 采集

static void *capture_thread(void *arg) {
  (void)arg;
  static int32_t raw[FRAME_SAMPLES * CAP_CH];
  static int16_t tmp[FRAME_SAMPLES * CAP_CH];
  // 直流偏置：ADC 原始值约 -5.5% FS。用极慢 IIR 跟踪，逐通道扣除，
  // 只跟随直流（~2Hz），不会像 80Hz 高通那样削掉语音低频。
  static double dc[MIC_CH] = {0};
  static int dc_init = 0;

  while (!g_stop) {
    int got = 0;
    /* 攒满一帧。ALSA buffer 只有 128 帧、period 160，readi 必然短读，
     * 所以这里必须能跨多次 readi 累积，按**帧**而不是按**样本**对齐。 */
    while (got < FRAME_SAMPLES && !g_stop) {
      int want = FRAME_SAMPLES - got;
      snd_pcm_sframes_t n = snd_pcm_readi(g_pcm_cap, raw + got * CAP_CH, want);
      if (n == -EPIPE) {
        snd_pcm_prepare(g_pcm_cap);
        snd_pcm_start(g_pcm_cap);
        got = 0;
        continue;
      }
      if (n < 0) {
        snd_pcm_recover(g_pcm_cap, (int)n, 1);
        usleep(2000);
        continue;
      }
      if (n == 0) continue;
      if ((int)n > want) n = want; /* 防御：不该发生，但别越界 */
      got += (int)n;
    }
    if (got < FRAME_SAMPLES) break;

    // S32 → S16，同时扣直流 + 预放大
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      for (int c = 0; c < CAP_CH; c++) {
        int idx = i * CAP_CH + c;
        if (c < MIC_CH) {
          double v = raw[idx];
          if (dc_init) {
            dc[c] += (v - dc[c]) * DC_TRACK_COEF;
          } else {
            dc[c] += v / FRAME_SAMPLES; /* 首帧取均值作初值 */
          }
          /* int32 → int16：先归一到 [-1,1) 再乘增益。
             直接把 int32 值乘增益再截断是错的 —— int32 满量程 2^31
             对应 S16 的 32768，中间差 65536 倍，会全量削顶。 */
          double s = (v - dc[c]) / 65536.0 * g_gain_mic;
          if (s > 32767.0) {
            s = 32767.0;
            g_clip++;
          }
          if (s < -32768.0) {
            s = -32768.0;
            g_clip++;
          }
          tmp[idx] = (int16_t)s;
        } else {
          // ch6 恒 0；ch7 是数字环回，按 16 位取高位，不做任何处理
          // （不给 ref 加增益、不过滤波器：它是 DAC 前的**精确**样本，
          //   任何额外处理都会破坏「ref == 写入的 PCM」这个不变量）
          tmp[idx] = (int16_t)(raw[idx] >> 16);
        }
      }
    }
    if (!dc_init) {
      logf_("DC init: %.0f %.0f %.0f %.0f %.0f %.0f", dc[0], dc[1], dc[2], dc[3], dc[4], dc[5]);
      dc_init = 1;
    }
    ring_write(&g_cap_ring, tmp, FRAME_SAMPLES * CAP_CH);
    g_cap_frames++;
    g_hb_cap++;
    if ((g_cap_frames & 15) == 0) { /* 每 160ms 刷新一次诊断快照 */
      memcpy(g_probe_snap, raw, sizeof(g_probe_snap));
      g_probe_ready = 1;
    }
  }
  return NULL;
}

// --------------------------------------------------------------------------- 处理

/* 选路：按（可选 post-AEC）能量挑一路。抽成函数，避免两条路径各抄一遍。 */
static int pick_loudest(int16_t x[][FRAME_SAMPLES]) {
  if (g_select_mode == 1) return g_fixed_ch;
  int best = 0;
  double bv = -1;
  for (int c = 0; c < MIC_CH; c++) {
    double p = 0;
    for (int i = 0; i < FRAME_SAMPLES; i++) p += (double)x[c][i] * x[c][i];
    if (p > bv) {
      bv = p;
      best = c;
    }
  }
  return best;
}

/* 自动选路必须在**消回声之前**做。
 * 之前按 AEC 输出能量选，等于偏向「这路没消干净」—— 消得好的通道被静音、
 * 消得差的通道被选中，上行里剩的就是残留回声。按输入能量选则偏向「离人近」，
 * 与回声抑制无关。固定通道（-c）仍然优先。 */

static void *process_thread(void *arg) {
  (void)arg;
  static int16_t in[FRAME_SAMPLES * CAP_CH];
  static int16_t near[MIC_CH][FRAME_SAMPLES];
  static int16_t refv[FRAME_SAMPLES];
  static int16_t avg_in[FRAME_SAMPLES];
  static int16_t cancel[MIC_CH][FRAME_SAMPLES];
  int16_t out[FRAME_SAMPLES];

  while (!g_stop) {
    g_hb_proc++;
    int got = ring_read(&g_cap_ring, in, FRAME_SAMPLES * CAP_CH, 200);
    if (got < FRAME_SAMPLES * CAP_CH) {
      if ((g_hb_proc & 63) == 63)
        logf_("proc 短路 got=%d/%d avail=%d", got, FRAME_SAMPLES * CAP_CH,
              ring_avail(&g_cap_ring));
      usleep(1000);
      continue;
    }

    // ① 解交织（直流与预放大已在采集端完成，这里不再重复处理）
    for (int c = 0; c < MIC_CH; c++) {
      for (int i = 0; i < FRAME_SAMPLES; i++) near[c][i] = in[i * CAP_CH + c];
    }
    for (int i = 0; i < FRAME_SAMPLES; i++) refv[i] = in[i * CAP_CH + REF_CH];

    // ② AEC + 选路
    //
    // ⚠️ 这里必须用 **MIC_CH 个独立 state**，不能用一个 state 循环调 6 次：
    //    speex_echo_cancellation 每次调用都会推进 st->x（参考延迟线）、
    //    更新同一组 W、并刷新 st->last_y，所以「一帧调 6 次」等于把延迟线
    //    推进了 6 帧、并让系数在 6 条不同回声路径之间来回拉扯 —— 不可能收敛。
    //    也不能用 init_mc：MC 版所有 mic 共享同一组自适应系数，而 6 只麦到
    //    喇叭的距离/房间响应各不相同。设备实测（自放自录，逐通道）：
    //        旧(1 state×6) 2.7dB  |  init_mc 0.6dB  |  6 个独立 state 5.2dB
    //    代价是多 5 次 far_end FFT —— 在 4×A53 上可接受，正确性优先。
    //    参考信号同帧喂入（ref 延迟实测必须为 0，见 AEC_REF_DELAY 注释）。
    if (g_aec_on && (g_echo || g_echo_mic[0])) {
      if (g_select_mode == 2) {
        // 六路平均后单通道 AEC（省 CPU）
        for (int i = 0; i < FRAME_SAMPLES; i++) {
          int acc = 0;
          for (int c = 0; c < MIC_CH; c++) acc += near[c][i];
          avg_in[i] = (int16_t)(acc / MIC_CH);
        }
        int16_t raw_in[FRAME_SAMPLES];
        memcpy(raw_in, avg_in, sizeof(raw_in));
        speex_echo_cancellation(g_echo, raw_in, refv, out);
        g_sel_ch = -1;
        double in_db = dbfs_s16(raw_in, FRAME_SAMPLES);
        double out_db = dbfs_s16(out, FRAME_SAMPLES);
        if (in_db > -70.0) { /* 只在有信号时统计，静音段 ERLE 无意义 */
          g_erle = in_db - out_db;
          g_erle_sum += g_erle;
          g_erle_n++;
        }
      } else {
        // 多通道：各自独立 AEC，选取后能量最大的一路。
        // state 与通道一一对应 —— 每个通道必须始终喂给同一个 state，
        // 否则等于重新收敛。g_echo_mic[c] 保证了这个绑定。
        for (int c = 0; c < MIC_CH; c++) {
          speex_echo_cancellation(g_echo_mic[c], near[c], refv, cancel[c]);
        }
        int best = pick_loudest(near); /* 按输入能量选，见 pick_loudest 上方注释 */
        g_sel_ch = best;
        memcpy(out, cancel[best], sizeof(out));

        /* ERLE 按「同一路 mic 前后」算，不能跨通道。
         * 旧实现用 near[best_new] 比 out[best_new]，而 best 每帧都可能变，
         * 于是 3.9dB 这种数字既不是 ch3 的也不是 ch4 的，无法用来判断收敛。 */
        double in_db = dbfs_s16(near[best], FRAME_SAMPLES);
        double out_db = dbfs_s16(cancel[best], FRAME_SAMPLES);
        if (in_db > -70.0) { /* 只在有信号时统计，静音段 ERLE 无意义 */
          g_erle = in_db - out_db;
          g_erle_sum += g_erle;
          g_erle_n++;
        }
      }
    } else {
      int best = pick_loudest(near);
      g_sel_ch = best;
      memcpy(out, near[best], sizeof(out));
    }

    // ③ 降噪
    if (g_ns_on && g_pp) speex_preprocess_run(g_pp, out);

    // ④ 后置增益
    if (g_gain_post_db) {
      double g = pow(10.0, g_gain_post_db / 20.0);
      for (int i = 0; i < FRAME_SAMPLES; i++) {
        double v = out[i] * g;
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        out[i] = (int16_t)v;
      }
    }

    g_frames++;
    ring_write(&g_up_ring, out, FRAME_SAMPLES);
  }
  return NULL;
}

// --------------------------------------------------------------------------- 网络

static int tcp_listen(int port) {
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return -1;
  int one = 1;
  setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  struct sockaddr_in a;
  memset(&a, 0, sizeof(a));
  a.sin_family = AF_INET;
  a.sin_addr.s_addr = htonl(INADDR_ANY);
  a.sin_port = htons((uint16_t)port);
  if (bind(fd, (struct sockaddr *)&a, sizeof(a)) < 0) {
    close(fd);
    return -1;
  }
  listen(fd, 4);
  return fd;
}

/* 下行无应用数据多久后踢掉会话。跟 keepalive 互补：
 * keepalive 杀「对端已死但 TCP 半开」；本超时杀「对端还活着但长期不发帧、
 * 占着唯一下行 session」——否则新 Mac 连上来只能堵在 accept 队列。 */
#define DOWN_IDLE_MS 30000

static void set_tcp_client_opts(int fd) {
  int one = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  /* keepalive：WiFi 闪断 / 对端崩溃不发 FIN 时，~2+1×3 ≈ 5s 内让 recv 失败，
     释放唯一的下行 session，避免僵尸连接占坑。 */
  setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
#ifdef TCP_KEEPIDLE
  {
    int idle = 2, intvl = 1, cnt = 3;
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
  }
#endif
  /* send 必须带超时：否则客户端读得慢（或断开未被探测）时，
     send 会阻塞几十秒，上行线程停摆，ring 迅速溢出。 */
  struct timeval tv;
  tv.tv_sec = 0;
  tv.tv_usec = 300000; /* 300ms */
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
}

/* 整帧写满。旧实现把 send 的部分写当作成功，下一帧从字节流中间接上，
 * 之后所有 PCM 永久错位半个样本 —— 听感是持续爆音，且极难定位。 */
static int send_all(int fd, const void *buf, size_t len) {
  const char *p = (const char *)buf;
  size_t off = 0;
  while (off < len) {
    ssize_t n = send(fd, p + off, len - off, MSG_NOSIGNAL);
    if (n < 0) {
      if (errno == EINTR) continue;
      if (errno == EAGAIN || errno == EWOULDBLOCK) return 0; /* 整帧丢弃 */
      return -1;
    }
    if (n == 0) return -1;
    off += (size_t)n;
  }
  return 1;
}

static void *uplink_thread(void *arg) {
  int lfd = *(int *)arg;
  static int16_t blk[FRAME_SAMPLES];
  while (!g_stop) {
    struct pollfd p = {lfd, POLLIN, 0};
    if (poll(&p, 1, 200) <= 0) continue;
    int cfd = accept(lfd, NULL, NULL);
    if (cfd < 0) continue;
    set_tcp_client_opts(cfd);
    logf_("上行已连接");
    ring_clear(&g_up_ring);

    while (!g_stop) {
      g_hb_up++;
      int got = ring_read(&g_up_ring, blk, FRAME_SAMPLES, 200);
      if (got < FRAME_SAMPLES) continue;
      if (g_mic_muted) memset(blk, 0, sizeof(blk));
      int r = send_all(cfd, blk, FRAME_BYTES_S16);
      if (r < 0) break;
      if (r == 0) {
        g_upsent_drop++; /* 客户端跟不上，丢这一帧，不阻塞 */
        continue;
      }
      g_upsent++;
    }
    close(cfd);
    logf_("上行断开，等待重连");
  }
  return NULL;
}

/* listen 上是否已有完成握手、等待 accept 的新连接。 */
static int listen_pending(int lfd) {
  struct pollfd p;
  p.fd = lfd;
  p.events = POLLIN;
  p.revents = 0;
  return poll(&p, 1, 0) > 0 && (p.revents & POLLIN);
}

/* 一次 accept，并抽干 backlog，只保留最新一条（旧连接直接丢掉）。
 * 场景：Mac 多次重连而设备还卡在旧 session 上时，队列里会堆多条 ESTABLISHED，
 * Recv-Q 里塞满 PCM，但唯一的下行线程读的还是旧 fd → Mac「播报成功」设备无声。 */
static int accept_latest(int lfd) {
  int cfd = accept(lfd, NULL, NULL);
  if (cfd < 0) return -1;
  for (;;) {
    if (!listen_pending(lfd)) break;
    int nc = accept(lfd, NULL, NULL);
    if (nc < 0) break;
    close(cfd);
    cfd = nc;
  }
  return cfd;
}

static void downlink_takeover_flush(void) {
  ring_clear(&g_play_ring);
  g_flush_req = 1; /* playback 线程 drop+prepare，丢掉旧音频 */
}

/* 下行会话：解析帧 → 写 play_ring / 请求 FLUSH / 更新音量。
 * 这里**不碰** g_pcm_play，所有 ALSA 操作归 playback 线程（见文件头所有权表）。
 *
 * lfd 一并传入：session 期间若有新连接到来，立刻让出（抢占），由线程循环
 * accept 最新连接。同一时刻只服务一条下行。 */
static void downlink_session(int cfd, int lfd, char **payload_buf, size_t *payload_cap) {
  int idle_ms = 0;
  for (;;) {
    if (listen_pending(lfd)) {
      logf_("下行被新连接抢占");
      return;
    }

    frame_hdr_t h;
    io_result_t r = io_read_full(cfd, &h, sizeof(h), &g_stop, 300);
    if (r == IO_TIMEOUT) {
      idle_ms += 300;
      if (idle_ms >= DOWN_IDLE_MS) {
        logf_("下行空闲超时 %ds，断开", DOWN_IDLE_MS / 1000);
        return;
      }
      continue;
    }
    if (r != IO_OK) return;
    idle_ms = 0;

    uint32_t len = hdr_get_length(&h);
    if (len > PROTO_MAX_PAYLOAD) {
      logf_("下行帧长度非法 %u，断开重连", len);
      return;
    }
    if (len > *payload_cap) {
      char *np = (char *)realloc(*payload_buf, len);
      if (!np) return;
      *payload_buf = np;
      *payload_cap = len;
    }
    if (len > 0) {
      if (listen_pending(lfd)) {
        logf_("下行被新连接抢占");
        return;
      }
      r = io_read_full(cfd, *payload_buf, len, &g_stop, 1000);
      if (r != IO_OK) return;
    }

    if (h.type == F_PCM) {
      int ns = (int)(len / 2);
      int16_t *p16 = (int16_t *)*payload_buf;
      /* 这里只做「解析并入 ring」。用户音量统一在 playback 线程施加，
         保证输出信号链只归一个线程管（也避免跨线程读 double g_vol）。 */
      ring_write(&g_play_ring, p16, ns);
      g_downframes++;
    } else if (h.type == F_FLUSH) {
      ring_clear(&g_play_ring);
      g_flush_req = 1; /* 交给 playback 线程去 drop+prepare */
      g_flushes++;
    } else if (h.type == F_VOLUME) {
      if (len >= 1) {
        /* F_VOLUME 字节是 Mac 侧的权威音量表达；转换成 pct 后交给
           set_vol_pct_from_byte，由它统一更新 g_vol_pct / g_vol / pct 文件。 */
        int byte = (unsigned char)(*payload_buf)[0];
        g_pending_vol_byte = byte;
      }
    } else if (h.type == F_PING) {
      ssize_t n = send(cfd, "PONG", 4, MSG_NOSIGNAL);
      (void)n;
    }
  }
}

static void *downlink_thread(void *arg) {
  int lfd = *(int *)arg;
  char *payload = NULL;
  size_t pcap = 0;

  while (!g_stop) {
    struct pollfd p = {lfd, POLLIN, 0};
    if (poll(&p, 1, 200) <= 0) continue;
    int cfd = accept_latest(lfd);
    if (cfd < 0) continue;
    set_tcp_client_opts(cfd);
    downlink_takeover_flush();
    logf_("下行已连接");
    downlink_session(cfd, lfd, &payload, &pcap);
    close(cfd);
    logf_("下行断开，等待重连");
  }
  free(payload);
  return NULL;
}

// --------------------------------------------------------------------------- 本地提示音
/* 提示音不走 play_ring：空闲时 ring 读超时会直接 continue，塞进 ring 常被丢掉。
   改为独立缓冲，在 ALSA 写出前混入；无下行 PCM 时也强制写一帧。 */

#if MUTE_SFX_MAX_LEN > VOL_DONG_LEN
#define DONG_MAX_SAMPLES MUTE_SFX_MAX_LEN
#else
#define DONG_MAX_SAMPLES VOL_DONG_LEN
#endif
static int16_t g_dong_buf[DONG_MAX_SAMPLES];
static int g_dong_len = 0;
static int g_dong_pos = 0;
static volatile int g_dong_kick = 0; /* 空闲起播时强制 prepare */
static pthread_mutex_t g_dong_mu = PTHREAD_MUTEX_INITIALIZER;
static int g_mic_mute_seen = -1; /* -1=尚未同步，边沿不响 */

/* 本地提示音总增益（相对原先响度） */
#define SFX_GAIN 0.5

static double sfx_amp_for_pct(int pct) { return jarvis_sfx_amp(pct) * SFX_GAIN; }

static void sfx_enqueue(const int16_t *pcm, int len, double amp) {
  if (!pcm || len <= 0) return;
  if (len > DONG_MAX_SAMPLES) len = DONG_MAX_SAMPLES;
  pthread_mutex_lock(&g_dong_mu);
  for (int i = 0; i < len; i++) {
    int v = (int)lround((double)pcm[i] * amp);
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    g_dong_buf[i] = (int16_t)v;
  }
  g_dong_len = len;
  g_dong_pos = 0;
  g_dong_kick = 1;
  pthread_mutex_unlock(&g_dong_mu);
}

static void vol_enqueue_dong(int pct) {
  (void)pct;
  /* 固定响度；Tink 极短，略抬一点以免被当成没声 */
  sfx_enqueue(VOL_DONG_PCM, VOL_DONG_LEN, 0.9 * SFX_GAIN);
}

/* ⚠️ 只允许 playback 线程调用：它读写静态边沿状态 g_mic_mute_seen 并会
 *    触发提示音。旧实现被 uplink 与 playback 同时调用，同一手势可能响两次
 *    或一次都不响，还会并发 fopen。uplink 只读镜像后的 g_mic_muted。 */
static void mute_poll_file(void) {
  int v = jarvis_read_int(JARVIS_MUTE_FILE, 0) ? 1 : 0;
  g_mic_muted = v;
  if (g_mic_mute_seen < 0) {
    g_mic_mute_seen = v;
    return;
  }
  if (v == g_mic_mute_seen) return;
  g_mic_mute_seen = v;
  int pct = g_vol_pct >= 0 ? g_vol_pct : 70;
  double amp = sfx_amp_for_pct(pct);
  if (v) {
    /* 禁用麦 → Bottle */
    sfx_enqueue(MUTE_SFX_OFF_PCM, MUTE_SFX_OFF_LEN, amp);
    logf_("麦克风禁用（Bottle）");
  } else {
    /* 启用麦 → Funk */
    sfx_enqueue(MUTE_SFX_ON_PCM, MUTE_SFX_ON_LEN, amp);
    logf_("麦克风启用（Funk）");
  }
}

static int vol_mix_dong(int16_t *mono, int n) {
  int mixed = 0;
  pthread_mutex_lock(&g_dong_mu);
  for (int i = 0; i < n && g_dong_pos < g_dong_len; i++) {
    int v = (int)mono[i] + (int)g_dong_buf[g_dong_pos++];
    if (v > 32767) v = 32767;
    if (v < -32768) v = -32768;
    mono[i] = (int16_t)v;
    mixed = 1;
  }
  if (g_dong_pos >= g_dong_len) {
    g_dong_len = 0;
    g_dong_pos = 0;
  }
  pthread_mutex_unlock(&g_dong_mu);
  return mixed;
}

static int vol_dong_pending(void) {
  pthread_mutex_lock(&g_dong_mu);
  int p = (g_dong_pos < g_dong_len);
  pthread_mutex_unlock(&g_dong_mu);
  return p;
}

/* 连续滑动算一次手势：响一次后上锁，停滑 ≥350ms 再解锁 */
#define VOL_SFX_IDLE_SEC 0.35
static int g_vol_sfx_armed = 1;
static double g_vol_last_change_sec = 0;

static double mono_sec(void) {
  struct timeval tv;
  gettimeofday(&tv, NULL);
  return (double)tv.tv_sec + (double)tv.tv_usec * 1e-6;
}

/* ⚠️ 只允许 playback 线程调用。它同时是 /tmp/jarvis-vol.pct 的**唯一写者**，
 *    因此 volumed / led-set.sh 读到的永远是 playback 实际生效的那个值。
 *    所有音量入路（F_VOLUME 字节、pct 文件）最终都收敛到这一个函数。 */
static void vol_set_pct(int pct, int announce) {
  pct = jarvis_vol_clamp(pct);
  if (pct == g_vol_pct) return;
  int first = (g_vol_pct < 0);
  g_vol_pct = pct;
  g_vol = jarvis_pct_gain(pct); /* 唯一的增益来源，与灯弧同一曲线 */
  g_vol_last_change_sec = mono_sec();
  jarvis_write_int(JARVIS_VOL_PCT_FILE, pct);
  if (first || !announce) return; /* 开机同步不响 */
  /* 同一手势只响一次；上一段还在播也不打断 */
  if (!g_vol_sfx_armed || vol_dong_pending()) return;
  g_vol_sfx_armed = 0;
  vol_enqueue_dong(pct);
  logf_("音量 %d%%（Tink）", pct);
}

static void vol_poll_file(void) {
  /* Mac 用 F_VOLUME 表达音量（权威）；只有从未收到过 F_VOLUME 时，
     才把 pct 文件当作初始值。这样设备端不会自己「回写-读回」形成环。 */
  if (g_pending_vol_byte >= 0) {
    int byte = g_pending_vol_byte;
    g_pending_vol_byte = -1;
    vol_set_pct(jarvis_byte_to_pct(byte), 1);
  } else if (g_vol_pct < 0) {
    int p = jarvis_read_int(JARVIS_VOL_PCT_FILE, -1);
    if (p >= 0) vol_set_pct(p, 0);
  }
  if (!g_vol_sfx_armed && (mono_sec() - g_vol_last_change_sec) >= VOL_SFX_IDLE_SEC) {
    g_vol_sfx_armed = 1;
  }
}

// --------------------------------------------------------------------------- 播放

static void *playback_thread(void *arg) {
  (void)arg;
  static int16_t mono[FRAME_SAMPLES];
  static int16_t stereo[FRAME_SAMPLES * 2];
  int prebuf = 0;
  unsigned tick = 0;

  while (!g_stop) {
    g_hb_play++;
    /* 本线程是所有状态文件的唯一轮询者（见文件头所有权表） */
    if ((tick++ % 5) == 0) {
      vol_poll_file(); /* ~50ms */
      mute_poll_file();
    }

    if (g_flush_req) {
      g_flush_req = 0;
      ring_clear(&g_play_ring);
      if (g_pcm_play) {
        /* drop 之后必须 prepare，否则下一次 writei 会持续失败 → 完全无声 */
        snd_pcm_drop(g_pcm_play);
        snd_pcm_prepare(g_pcm_play);
      }
      prebuf = 0;
    }

    int dong = vol_dong_pending();
    if (!prebuf && !dong) {
      if (ring_avail(&g_play_ring) < PLAY_PREBUF * FRAME_SAMPLES) {
        usleep(1000);
        continue;
      }
      prebuf = 1;
    }

    memset(mono, 0, sizeof(mono));
    int got = 0;
    if (prebuf || ring_avail(&g_play_ring) > 0) {
      got = ring_read(&g_play_ring, mono, FRAME_SAMPLES, dong ? 0 : 50);
      if (got < FRAME_SAMPLES) {
        if (ring_avail(&g_play_ring) == 0) prebuf = 0;
        if (!dong && got <= 0) continue;
        /* 有咚：不足一帧的部分保持 0，照样送 ALSA */
      }
    } else if (!dong) {
      usleep(1000);
      continue;
    }

    int mixed = vol_mix_dong(mono, FRAME_SAMPLES);
    if (got < FRAME_SAMPLES && !mixed && !vol_dong_pending()) {
      /* 既没 PCM 也没咚 */
      usleep(1000);
      continue;
    }

    if (g_dong_kick && g_pcm_play) {
      g_dong_kick = 0;
      /* 已在跑就不要 prepare：会丢掉短促提示音（Tink 有效声仅几十 ms） */
      snd_pcm_state_t st = snd_pcm_state(g_pcm_play);
      if (st != SND_PCM_STATE_RUNNING && st != SND_PCM_STATE_PREPARED) {
        snd_pcm_prepare(g_pcm_play);
      }
    }

    // 硬件只播 index1，但两声道都写（与实测保持一致）
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      /* 用户音量在这里施加 —— 全链路唯一的增益点。
         Mac 侧的 peak_db 是 TTS 响度归一化，与用户音量正交，不能替代。 */
      double v = (double)mono[i] * g_vol;
      if (v > 32767.0) v = 32767.0;
      if (v < -32768.0) v = -32768.0;
      int16_t s = (int16_t)v;
      stereo[i * 2] = s;
      stereo[i * 2 + 1] = s;
    }
    int off = 0;
    while (off < FRAME_SAMPLES && !g_stop) {
      snd_pcm_sframes_t n = snd_pcm_writei(g_pcm_play, stereo + off * 2, FRAME_SAMPLES - off);
      if (n == -EPIPE || n == -ESTRPIPE) {
        if (snd_pcm_prepare(g_pcm_play) < 0) break;
        continue; /* 同一帧重试，不要丢掉 */
      }
      if (n < 0) {
        if (snd_pcm_recover(g_pcm_play, (int)n, 1) < 0) {
          logf_("播放 writei 失败: %s", snd_strerror((int)n));
          break;
        }
        continue;
      }
      off += (int)n;
    }
  }
  return NULL;
}

// --------------------------------------------------------------------------- ALSA

/* 采样率用 set_rate 而非 set_rate_near，并**硬断言**回填值。
 * 旧实现用 near 且丢弃返回值：万一固件只给 48k，后续全都按 16k 解释，
 * 帧长/AEC/时间戳一起错 3 倍，而且不会有任何报错。 */
static int apply_rate(snd_pcm_t *pcm, snd_pcm_hw_params_t *hw, unsigned want,
                      const char *what) {
  int dir = 0;
  int rc = snd_pcm_hw_params_set_rate(pcm, hw, want, dir);
  if (rc < 0) {
    logf_("%s: 不支持精确 %u Hz（%s）——拒绝以降级采样率运行", what, want,
          snd_strerror(rc));
    return -1;
  }
  unsigned got = 0;
  snd_pcm_hw_params_get_rate(hw, &got, &dir);
  if (got != want) {
    logf_("%s: 采样率回填 %u != 请求 %u ——拒绝继续", what, got, want);
    return -1;
  }
  g_rate_ok = 1;
  return 0;
}

static int open_capture(void) {
  int rc = snd_pcm_open(&g_pcm_cap, "hw:0,0", SND_PCM_STREAM_CAPTURE, 0);
  if (rc < 0) {
    logf_("打开采集失败: %s", snd_strerror(rc));
    return -1;
  }
  snd_pcm_hw_params_t *hw;
  snd_pcm_hw_params_alloca(&hw);
  snd_pcm_hw_params_any(g_pcm_cap, hw);
  snd_pcm_hw_params_set_access(g_pcm_cap, hw, SND_PCM_ACCESS_RW_INTERLEAVED);
  snd_pcm_hw_params_set_format(g_pcm_cap, hw, SND_PCM_FORMAT_S32_LE);
  unsigned ch = CAP_CH;
  snd_pcm_hw_params_set_channels_near(g_pcm_cap, hw, &ch);
  if (ch != CAP_CH) {
    logf_("采集只给了 %u 通道（需要 %d）", ch, CAP_CH);
    return -1;
  }
  if (apply_rate(g_pcm_cap, hw, SR, "采集") < 0) return -1;
  int dir = 0;
  snd_pcm_uframes_t period = FRAME_SAMPLES;
  snd_pcm_uframes_t buffer = FRAME_SAMPLES * 5;
  snd_pcm_hw_params_set_period_size_near(g_pcm_cap, hw, &period, &dir);
  snd_pcm_hw_params_set_buffer_size_near(g_pcm_cap, hw, &buffer);
  rc = snd_pcm_hw_params(g_pcm_cap, hw);
  if (rc < 0) {
    logf_("采集参数失败: %s", snd_strerror(rc));
    return -1;
  }
  snd_pcm_prepare(g_pcm_cap);
  snd_pcm_start(g_pcm_cap);
  logf_("采集 %uch@%uHz period=%lu buffer=%lu", ch, SR, (unsigned long)period,
        (unsigned long)buffer);
  return 0;
}

static int open_playback(void) {
  int rc = snd_pcm_open(&g_pcm_play, "hw:0,0", SND_PCM_STREAM_PLAYBACK, 0);
  if (rc < 0) {
    logf_("打开播放失败: %s", snd_strerror(rc));
    return -1;
  }
  snd_pcm_hw_params_t *hw;
  snd_pcm_hw_params_alloca(&hw);
  snd_pcm_hw_params_any(g_pcm_play, hw);
  snd_pcm_hw_params_set_access(g_pcm_play, hw, SND_PCM_ACCESS_RW_INTERLEAVED);
  snd_pcm_hw_params_set_format(g_pcm_play, hw, SND_PCM_FORMAT_S16_LE);
  unsigned ch = 2;
  snd_pcm_hw_params_set_channels_near(g_pcm_play, hw, &ch);
  if (apply_rate(g_pcm_play, hw, SR, "播放") < 0) return -1;
  int dir = 0;
  snd_pcm_uframes_t period = FRAME_SAMPLES;
  snd_pcm_uframes_t period_saved = period;
  snd_pcm_uframes_t buffer = FRAME_SAMPLES * 8;
  snd_pcm_hw_params_set_period_size_near(g_pcm_play, hw, &period, &dir);
  snd_pcm_hw_params_set_buffer_size_near(g_pcm_play, hw, &buffer);
  rc = snd_pcm_hw_params(g_pcm_play, hw);
  if (rc < 0) {
    logf_("播放参数失败: %s", snd_strerror(rc));
    return -1;
  }
  snd_pcm_prepare(g_pcm_play);
  logf_("播放 %uch@%uHz period=%lu(buffer 目标 %lu) buffer=%lu", ch, SR,
        (unsigned long)period, (unsigned long)period_saved, (unsigned long)buffer);
  return 0;
}

// --------------------------------------------------------------------------- 统计

static void dump_stats(void) {
  double erle_avg = g_erle_n ? g_erle_sum / g_erle_n : 0;
  logf_("STATS cap=%lu frames=%lu up=%lu drop=%lu down=%lu flush=%lu "
        "drop[cap=%lu up=%lu play=%lu] clip=%lu sel=%d erle=%.1f/%.1f "
        "hb[%lu/%lu/%lu/%lu]",
        g_cap_frames, g_frames, g_upsent, g_upsent_drop, g_downframes, g_flushes,
        g_cap_ring.dropped, g_up_ring.dropped, g_play_ring.dropped, g_clip, g_sel_ch,
        erle_avg, g_erle, g_hb_cap, g_hb_proc, g_hb_up, g_hb_play);
}

// --------------------------------------------------------------------------- main

static void usage(const char *p) {
  fprintf(stderr,
          "用法: %s [选项]\n"
          "  -c N   固定麦克风通道 0-5（默认自适应选路）\n"
          "  -a     ch0-5 六路平均后单通道 AEC（省 CPU）\n"
          "  -g X   麦克风数字增益（默认 20，避开 AEC 定点饱和；要更响用 -p）\n"
          "  -p DB  后置增益 dB（默认 0）\n"
          "  -x     关闭 AEC\n"
          "  -n     关闭降噪（默认开）\n"
          "  -A     开启 AGC（默认关；勿与大固定增益叠用）\n"
          "  -q     安静模式\n"
          "  -h     帮助\n",
          p);
}

int main(int argc, char **argv) {
  int opt;
  while ((opt = getopt(argc, argv, "c:ag:p:xnAqh")) != -1) {
    switch (opt) {
      case 'c':
        g_select_mode = 1;
        g_fixed_ch = atoi(optarg);
        if (g_fixed_ch < 0 || g_fixed_ch >= MIC_CH) {
          fprintf(stderr, "-c 只接受 0..%d\n", MIC_CH - 1);
          return 1;
        }
        break;
      case 'a': g_select_mode = 2; break;
      case 'g': g_gain_mic = atof(optarg); break;
      case 'p': g_gain_post_db = atoi(optarg); break;
      case 'x': g_aec_on = 0; break;
      case 'n': g_ns_on = 0; break;
      case 'A': g_agc_on = 1; break;
      case 'q': g_verbose = 0; break;
      case 'h': usage(argv[0]); return 0;
      default: usage(argv[0]); return 1;
    }
  }

  if (pipe(g_sig_pipe) != 0) {
    fprintf(stderr, "pipe 失败: %s\n", strerror(errno));
    return 1;
  }

  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  signal(SIGUSR1, on_usr1);
  signal(SIGUSR2, on_usr2);
  signal(SIGPIPE, SIG_IGN);

  if (ring_init(&g_cap_ring, FRAME_SAMPLES * CAP_CH * 8) < 0) return 1;
  if (ring_init(&g_up_ring, FRAME_SAMPLES * UP_RING_FRAMES) < 0) return 1;
  if (ring_init(&g_play_ring, FRAME_SAMPLES * PLAY_RING_FRAMES) < 0) return 1;

  if (g_aec_on) {
    if (g_select_mode == 2) {
      /* 平均模式：一路 AEC */
      g_echo = speex_echo_state_init(FRAME_SAMPLES, AEC_FILTER_LEN);
      if (!g_echo) {
        logf_("AEC 初始化失败");
        return 1;
      }
      int sr = SR;
      speex_echo_ctl(g_echo, SPEEX_ECHO_SET_SAMPLING_RATE, &sr);
    } else {
      /* 多通道模式：通道 c 固定使用 g_echo_mic[c]，互不干扰 */
      for (int c = 0; c < MIC_CH; c++) {
        g_echo_mic[c] = speex_echo_state_init(FRAME_SAMPLES, AEC_FILTER_LEN);
        if (!g_echo_mic[c]) {
          logf_("AEC 初始化失败（ch%d）", c);
          for (int k = 0; k < c; k++) speex_echo_state_destroy(g_echo_mic[k]);
          return 1;
        }
        int sr = SR;
        speex_echo_ctl(g_echo_mic[c], SPEEX_ECHO_SET_SAMPLING_RATE, &sr);
      }
    }
  }

  if (g_ns_on) {
    g_pp = speex_preprocess_state_init(FRAME_SAMPLES, SR);
    if (g_pp) {
      /* 设备上的 libspeexdsp.so.1.5.0 是厂商自建的精简版（53KB，2023-12-06），
       * **不支持 AGC**：SPEEX_PREPROCESS_SET_AGC(2) 与 SET_AGC_TARGET(46) 都返回 -1。
       * 而容器里 Debian jessie 的 1.2~rc1.2 支持。所以不能假设「编过 = 能用」，
       * 每个 ctl 都要检查返回值，不支持的只报一次，不要刷屏。 */
      int one = 1, zero = 0;
      if (speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_DENOISE, &one) < 0)
        logf_("降噪：设备库不支持 SET_DENOISE");
      if (speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_VAD, &zero) < 0)
        logf_("降噪：设备库不支持 SET_VAD");
      /* 固定增益已把说话抬到合适电平；降噪再压噪声底，避免远场底噪被听见 */
      {
        int ns = -20;
        if (speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_NOISE_SUPPRESS, &ns) < 0)
          logf_("降噪：设备库不支持 SET_NOISE_SUPPRESS");
      }
      // AGC 默认关：大固定增益 + AGC 容易把噪声底也爬起来。
      int agc = g_agc_on ? 1 : 0;
      if (speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC, &agc) < 0) {
        if (g_agc_on)
          logf_("⚠️ 设备库不支持 AGC（-A 无效）；用 -p <dB> 做后置增益代替");
        else
          logf_("提示：设备库不支持 AGC，AGC 恒为关（不影响默认路径）");
      } else if (g_agc_on) {
        int target = 12000; /* 约 -8.7 dBFS，留余量防削顶 */
        int inc = 12, dec = 6;
        int maxgain = 16384;
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC_TARGET, &target);
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC_INCREMENT, &inc);
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC_DECREMENT, &dec);
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC_MAX_GAIN, &maxgain);
      }
      /* 单通道模式下把对应 AEC 接到预处理（normalise 会据此避开回声频点）。
         多通道模式下各通道系数不同，无法共用，交由 speex 自己处理即可。 */
      if (g_aec_on) {
        SpeexEchoState *for_pp = (g_select_mode == 2)
                                     ? g_echo
                                     : g_echo_mic[g_select_mode == 1 ? g_fixed_ch : 0];
        if (for_pp) speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_ECHO_STATE, for_pp);
      }
    }
  }

  if (open_capture() < 0 || open_playback() < 0) return 1;

  int lfd_up = tcp_listen(UP_PORT);
  int lfd_down = tcp_listen(DOWN_PORT);
  if (lfd_up < 0 || lfd_down < 0) {
    logf_("监听失败 up=%d down=%d errno=%d(%s)", lfd_up, lfd_down, errno, strerror(errno));
    return 1;
  }

  pthread_t t1, t2, t3, t4, t5;
  pthread_create(&t1, NULL, capture_thread, NULL);
  pthread_create(&t2, NULL, process_thread, NULL);
  pthread_create(&t3, NULL, uplink_thread, &lfd_up);
  pthread_create(&t4, NULL, downlink_thread, &lfd_down);
  pthread_create(&t5, NULL, playback_thread, NULL);

  logf_("就绪 up=%d down=%d AEC=%d(%s) NS=%d 选路=%d gain=%.2f post=%ddB",
        UP_PORT, DOWN_PORT, g_aec_on,
        g_select_mode == 2 ? "单通道(平均)" : g_select_mode == 1 ? "固定通道" : "6路独立",
        g_ns_on, g_select_mode, g_gain_mic, g_gain_post_db);

  /* 事件循环：等待自管道（SIGUSR1/SIGUSR2）或 30s 周期，全部打印都在这里做 */
  while (!g_stop) {
    struct pollfd p = {g_sig_pipe[0], POLLIN, 0};
    int pr = poll(&p, 1, 1000);
    if (pr > 0 && (p.revents & POLLIN)) {
      char buf[64];
      ssize_t n = read(g_sig_pipe[0], buf, sizeof(buf));
      if (n > 0) {
        for (ssize_t i = 0; i < n; i++) {
          if (buf[i] == 'S') g_stats_req = 1;
          if (buf[i] == 'P') g_probe_req = 1;
        }
      }
    }
    if (g_stats_req) {
      g_stats_req = 0;
      dump_stats();
    }
    if (g_probe_req) {
      g_probe_req = 0;
      probe_channels();
    }
    static unsigned tick_stats = 0;
    if (++tick_stats >= 30) {
      tick_stats = 0;
      dump_stats();
    }
  }

  logf_("退出中…");
  close(lfd_up);
  close(lfd_down);
  /* 先 shutdown 监听 socket 让 accept 立即返回，再 join。
     线程本身都跑在带超时的 poll / recv / ring_read 上，1s 内必退。 */
  pthread_join(t1, NULL);
  pthread_join(t2, NULL);
  pthread_join(t3, NULL);
  pthread_join(t4, NULL);
  pthread_join(t5, NULL);

  if (g_pcm_cap) snd_pcm_close(g_pcm_cap);
  if (g_pcm_play) snd_pcm_close(g_pcm_play);
  if (g_echo) speex_echo_state_destroy(g_echo);
  for (int c = 0; c < MIC_CH; c++) {
    if (g_echo_mic[c]) speex_echo_state_destroy(g_echo_mic[c]);
  }
  if (g_pp) speex_preprocess_state_destroy(g_pp);
  ring_free(&g_cap_ring);
  ring_free(&g_up_ring);
  ring_free(&g_play_ring);
  if (g_sig_pipe[0] >= 0) close(g_sig_pipe[0]);
  if (g_sig_pipe[1] >= 0) close(g_sig_pipe[1]);
  logf_("已退出");
  return 0;
}