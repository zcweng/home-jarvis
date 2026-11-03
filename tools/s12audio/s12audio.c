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
// 重要：设备上 libspeexdsp 1.2-rc1 是**定点**版，echo/preprocess 全部用 spx_int16_t。
// 所以内部处理链一律 S16，不转 float —— 既符合库接口，又比 float 省 CPU。
//
// 线程模型：
//   capture  → cap_ring(8ch S16) → process(解交织+HPF+AEC+NS+选路) → up_ring → uplink
//   downlink → play_ring(mono S16) → playback → ALSA
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

// --------------------------------------------------------------------------- 常量

#define CAP_CH 8
#define MIC_CH 6
#define REF_CH 7

#define SR 16000
#define FRAME_MS 10
#define FRAME_SAMPLES (SR / 1000 * FRAME_MS) /* 160 */
#define FRAME_BYTES_S16 (FRAME_SAMPLES * 2)  /* 320 */

#define AEC_FILTER_LEN 3200 /* 200ms @16k，speex 要求是 16 的倍数 */

#define HPF_CUTOFF_HZ 80.0

#define UP_PORT 8765
#define DOWN_PORT 8766

#define PLAY_PREBUF 3 /* 起播前攒 30ms */
#define PLAY_RING_FRAMES 300 /* 3s@10ms：抗住客户端短时突发，避免挤掉开头 */

#define F_PCM 1
#define F_FLUSH 2
#define F_VOLUME 3
#define F_PING 4

typedef struct __attribute__((packed)) {
  uint8_t type;
  uint8_t flags;
  uint16_t reserved;
  uint32_t length;
} frame_hdr_t;

// --------------------------------------------------------------------------- 全局

static volatile sig_atomic_t g_stop = 0;
static int g_verbose = 1;

/* 2026-11-02 重标定：噪声底 ≈ -84 dBFS，正常说话峰值 ≈ -53 dBFS（ch3 最佳）。
   目标说话后置 -18 dBFS → gain≈53；取 56 留一点余量。
   旧值 265（+48dB）是把噪声底误当成语音，导致噪声抬到 -35dBFS 且大量削顶。 */
static double g_gain_mic = 56.0;
static int g_select_mode = 0; /* 0=自适应 1=固定 2=平均 */
static int g_fixed_ch = 0;
static int g_aec_on = 1;
static int g_ns_on = 1;
static int g_agc_on = 0;
static int g_gain_post_db = 0;
static double g_vol = 1.0;

static unsigned long g_upsent_drop;
static unsigned long g_frames, g_upsent, g_downframes, g_flushes, g_overflows;
static unsigned long g_cap_frames;
static volatile unsigned long g_hb_proc, g_hb_up, g_hb_play, g_hb_cap;
static volatile sig_atomic_t g_do_probe = 0;
static unsigned long g_clip = 0;
static double g_erle = 0, g_erle_sum = 0;
static unsigned long g_erle_n = 0;
static int g_sel_ch = 0;

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

static void on_signal(int s) {
  (void)s;
  g_stop = 1;
}

static void probe_channels(void);

static void dump_stats(void) {
  double erle_avg = g_erle_n ? g_erle_sum / g_erle_n : 0;
  logf_("STATS cap=%lu frames=%lu up=%lu drop=%lu down=%lu flush=%lu ovf=%lu clip=%lu sel=%d erle=%.1f/%.1f hb[%lu/%lu/%lu/%lu]",
        g_cap_frames, g_frames, g_upsent, g_upsent_drop, g_downframes, g_flushes, g_overflows, g_clip,
        g_sel_ch, erle_avg, g_erle, g_hb_cap, g_hb_proc, g_hb_up, g_hb_play);
}

static void on_usr1(int s) {
  (void)s;
  dump_stats();
}

static void on_usr2(int s) {
  (void)s;
  g_do_probe = 1;
}

static double dbfs_s16(const int16_t *x, int n) {
  double s = 0;
  for (int i = 0; i < n; i++) {
    double v = x[i] / 32768.0;
    s += v * v;
  }
  if (n <= 0) return -120.0;
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

// --------------------------------------------------------------------------- 环形缓冲（S16）

typedef struct {
  int16_t *buf;
  int cap;
  int rd;
  int wr;
  int count; /* 当前有效样本数，避免用指针差值推算（覆盖时会算成负数） */
  pthread_mutex_t m;
  pthread_cond_t c;
} ring_t;

static int ring_init(ring_t *r, int cap) {
  r->buf = calloc(cap, sizeof(int16_t));
  if (!r->buf) return -1;
  r->cap = cap;
  r->rd = r->wr = 0;
  r->count = 0;
  pthread_mutex_init(&r->m, NULL);
  pthread_cond_init(&r->c, NULL);
  return 0;
}

static void ring_free(ring_t *r) {
  free(r->buf);
  pthread_mutex_destroy(&r->m);
  pthread_cond_destroy(&r->c);
}

static void ring_clear(ring_t *r) {
  pthread_mutex_lock(&r->m);
  r->rd = r->wr = 0;
  r->count = 0;
  pthread_cond_signal(&r->c);
  pthread_mutex_unlock(&r->m);
}

static int ring_avail(ring_t *r) {
  pthread_mutex_lock(&r->m);
  int n = r->count;
  pthread_mutex_unlock(&r->m);
  return n;
}

static void ring_write(ring_t *r, const int16_t *x, int n) {
  pthread_mutex_lock(&r->m);
  for (int i = 0; i < n; i++) {
    if (r->count >= r->cap) { /* 满：丢最旧一个 */
      r->rd = (r->rd + 1) % r->cap;
      r->count--;
      g_overflows++;
    }
    r->buf[r->wr] = x[i];
    r->wr = (r->wr + 1) % r->cap;
    r->count++;
  }
  pthread_cond_signal(&r->c);
  pthread_mutex_unlock(&r->m);
}

// 读满 n 个样本；超时返回实际读到的。
// 刻意不用条件变量：帧率只有 100Hz（10ms），10ms 级轮询的开销可忽略，
// 但能彻底避免"丢失唤醒 → 永久阻塞"这类只在嵌入式老 glibc 上出现的坑。
static int ring_read(ring_t *r, int16_t *x, int n, int timeout_ms) {
  int deadline_us = timeout_ms * 1000;
  int waited = 0;
  int got = 0;
  while (got < n && !g_stop) {
    pthread_mutex_lock(&r->m);
    int avail = r->count;
    if (avail > 0) {
      int take = n - got;
      if (take > avail) take = avail;
      int first = r->cap - r->rd;
      if (first > take) first = take;
      memcpy(x + got, r->buf + r->rd, (size_t)first * sizeof(int16_t));
      r->rd = (r->rd + first) % r->cap;
      r->count -= first;
      got += first;
      if (take > first) {
        int rest = take - first;
        memcpy(x + got, r->buf + r->rd, (size_t)rest * sizeof(int16_t));
        r->rd = (r->rd + rest) % r->cap;
        r->count -= rest;
        got += rest;
      }
    }
    pthread_mutex_unlock(&r->m);

    if (got >= n) break;
    if (waited >= deadline_us) break;
    usleep(2000);
    waited += 2000;
  }
  return got;
}

// --------------------------------------------------------------------------- 状态

static snd_pcm_t *g_pcm_cap = NULL;
static snd_pcm_t *g_pcm_play = NULL;

static ring_t g_cap_ring;  /* 8ch 交织 S16 */
static ring_t g_up_ring;   /* mono S16 待上行 */
static ring_t g_play_ring; /* mono S16 待播放 */

static SpeexEchoState *g_echo = NULL;
static SpeexPreprocessState *g_pp = NULL;

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
      // S32 → S16，同时扣直流 + 预放大
      for (int i = 0; i < (int)n; i++) {
        for (int c = 0; c < CAP_CH; c++) {
          int idx = (got + i) * CAP_CH + c;
          if (c < MIC_CH) {
            double v = raw[idx];
            if (dc_init) {
              dc[c] += (v - dc[c]) * 0.0005;
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
            tmp[idx] = (int16_t)(raw[idx] >> 16);
          }
        }
      }
      got += (int)n;
    }
    if (got == FRAME_SAMPLES) {
      if (!dc_init) {
        logf_("DC init: %.0f %.0f %.0f %.0f %.0f %.0f", dc[0], dc[1], dc[2], dc[3], dc[4], dc[5]);
      }
      dc_init = 1;
      ring_write(&g_cap_ring, tmp, FRAME_SAMPLES * CAP_CH);
      g_cap_frames++;
      g_hb_cap++;
      if ((g_cap_frames & 15) == 0) { /* 每 160ms 刷新一次诊断快照 */
        memcpy(g_probe_snap, raw, sizeof(g_probe_snap));
        g_probe_ready = 1;
      }
    }
  }
  return NULL;
}

// --------------------------------------------------------------------------- 处理

static void *process_thread(void *arg) {
  (void)arg;
  static int16_t in[FRAME_SAMPLES * CAP_CH];
  static int16_t near[MIC_CH][FRAME_SAMPLES];
  static int16_t refv[FRAME_SAMPLES];
  static int16_t ref_avg[FRAME_SAMPLES];
  static int16_t cancel[MIC_CH][FRAME_SAMPLES];
  int16_t out[FRAME_SAMPLES];

  while (!g_stop) {
    g_hb_proc++;
    int got = ring_read(&g_cap_ring, in, FRAME_SAMPLES * CAP_CH, 200);
    if (got < FRAME_SAMPLES * CAP_CH) {
      if ((g_hb_proc & 63) == 63) logf_("proc短路 got=%d/%d avail=%d", got, FRAME_SAMPLES * CAP_CH, ring_avail(&g_cap_ring));
      usleep(1000);
      continue;
    }

    // ① 解交织（直流与预放大已在采集端完成，这里不再重复处理）
    for (int c = 0; c < MIC_CH; c++) {
      for (int i = 0; i < FRAME_SAMPLES; i++) near[c][i] = in[i * CAP_CH + c];
    }
    for (int i = 0; i < FRAME_SAMPLES; i++) refv[i] = in[i * CAP_CH + REF_CH];

    // ② AEC + 选路
    if (g_aec_on && g_echo) {
      if (g_select_mode == 2) {
        // 六路平均后单通道 AEC（注意：out 同时作输入和输出在 speex 里不安全，用独立缓冲）
        for (int i = 0; i < FRAME_SAMPLES; i++) {
          int acc = 0;
          for (int c = 0; c < MIC_CH; c++) acc += near[c][i];
          ref_avg[i] = (int16_t)(acc / MIC_CH);
        }
        int16_t raw_in[FRAME_SAMPLES];
        memcpy(raw_in, ref_avg, sizeof(raw_in));
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
        // 多通道：各自 AEC，选取后能量最大的一路
        for (int c = 0; c < MIC_CH; c++) speex_echo_cancellation(g_echo, near[c], refv, cancel[c]);
        int best = 0;
        if (g_select_mode == 1) {
          best = g_fixed_ch;
        } else {
          double bv = -1;
          for (int c = 0; c < MIC_CH; c++) {
            double p = 0;
            for (int i = 0; i < FRAME_SAMPLES; i++) p += (double)cancel[c][i] * cancel[c][i];
            if (p > bv) {
              bv = p;
              best = c;
            }
          }
        }
        g_sel_ch = best;
        memcpy(out, cancel[best], sizeof(out));

        double in_db = dbfs_s16(near[best], FRAME_SAMPLES);
        double out_db = dbfs_s16(out, FRAME_SAMPLES);
        if (in_db > -70.0) {
          g_erle = in_db - out_db;
          g_erle_sum += g_erle;
          g_erle_n++;
        }
      }
    } else {
      int best = 0;
      if (g_select_mode == 1) {
        best = g_fixed_ch;
      } else {
        double bv = -1;
        for (int c = 0; c < MIC_CH; c++) {
          double p = 0;
          for (int i = 0; i < FRAME_SAMPLES; i++) p += (double)near[c][i] * near[c][i];
          if (p > bv) {
            bv = p;
            best = c;
          }
        }
      }
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

static void set_nodelay(int fd) {
  int one = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  /* send 必须带超时：否则客户端读得慢（或断开未被探测）时，
     send 会阻塞几十秒，上行线程停摆，ring 迅速溢出。 */
  struct timeval tv;
  tv.tv_sec = 0;
  tv.tv_usec = 300000; /* 300ms */
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
}

static void *uplink_thread(void *arg) {
  int lfd = *(int *)arg;
  while (!g_stop) {
    struct pollfd p = {lfd, POLLIN, 0};
    if (poll(&p, 1, 200) <= 0) continue;
    int cfd = accept(lfd, NULL, NULL);
    if (cfd < 0) continue;
    set_nodelay(cfd);
    logf_("上行已连接");
    ring_clear(&g_up_ring);

    static int16_t blk[FRAME_SAMPLES];
    while (!g_stop) {
      g_hb_up++;
      int got = ring_read(&g_up_ring, blk, FRAME_SAMPLES, 200);
      if (got < FRAME_SAMPLES) continue;
      ssize_t n = send(cfd, blk, FRAME_BYTES_S16, MSG_NOSIGNAL);
      if (n < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
          g_upsent_drop++; /* 客户端跟不上，丢这一帧，不阻塞 */
          continue;
        }
        break;
      }
      if (n == 0) break;
      g_upsent++;
    }
    close(cfd);
    logf_("上行断开，等待重连");
  }
  return NULL;
}

static void *downlink_thread(void *arg) {
  int lfd = *(int *)arg;
  char *payload = NULL;
  size_t pcap = 0;

  while (!g_stop) {
    struct pollfd p = {lfd, POLLIN, 0};
    if (poll(&p, 1, 200) <= 0) continue;
    int cfd = accept(lfd, NULL, NULL);
    if (cfd < 0) continue;
    set_nodelay(cfd);
    logf_("下行已连接");

    while (!g_stop) {
      frame_hdr_t h;
      size_t got = 0;
      while (got < sizeof(h)) {
        ssize_t n = recv(cfd, ((char *)&h) + got, sizeof(h) - got, 0);
        if (n <= 0) goto sess_done;
        got += (size_t)n;
      }
      if (h.length > 1024u * 1024u) goto sess_done;
      if (h.length > pcap) {
        char *np = realloc(payload, h.length);
        if (!np) goto sess_done;
        payload = np;
        pcap = h.length;
      }
      got = 0;
      while (got < h.length) {
        ssize_t n = recv(cfd, payload + got, h.length - got, 0);
        if (n <= 0) goto sess_done;
        got += (size_t)n;
      }

      if (h.type == F_PCM) {
        int ns = (int)(h.length / 2);
        int16_t *p16 = (int16_t *)payload;
        if (g_vol != 1.0) {
          for (int i = 0; i < ns; i++) p16[i] = (int16_t)(p16[i] * g_vol);
        }
        ring_write(&g_play_ring, p16, ns);
        g_downframes++;
      } else if (h.type == F_FLUSH) {
        ring_clear(&g_play_ring);
        if (g_pcm_play) {
          /* drop 之后必须 prepare，否则下一次 writei 会持续失败 → 完全无声 */
          snd_pcm_drop(g_pcm_play);
          snd_pcm_prepare(g_pcm_play);
        }
        g_flushes++;
      } else if (h.type == F_VOLUME) {
        if (h.length >= 1) {
          int v = (unsigned char)payload[0];
          if (v > 100) v = 100;
          g_vol = v <= 0 ? 0.001 : pow(10.0, (v - 60) / 20.0);
        }
      } else if (h.type == F_PING) {
        send(cfd, "PONG", 4, MSG_NOSIGNAL);
      }
    }
  sess_done:
    close(cfd);
    logf_("下行断开，等待重连");
  }
  free(payload);
  return NULL;
}

// --------------------------------------------------------------------------- 播放

static void *playback_thread(void *arg) {
  (void)arg;
  static int16_t mono[FRAME_SAMPLES];
  static int16_t stereo[FRAME_SAMPLES * 2];
  int prebuf = 0;

  while (!g_stop) {
    g_hb_play++;
    if (!prebuf) {
      if (ring_avail(&g_play_ring) < PLAY_PREBUF * FRAME_SAMPLES) {
        usleep(1000);
        continue;
      }
      prebuf = 1;
    }
    int got = ring_read(&g_play_ring, mono, FRAME_SAMPLES, 50);
    if (got < FRAME_SAMPLES) {
      if (ring_avail(&g_play_ring) == 0) prebuf = 0;
      continue;
    }
    // 硬件只播 index1，但两声道都写（与实测保持一致）
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      stereo[i * 2] = mono[i];
      stereo[i * 2 + 1] = mono[i];
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
  unsigned rate = SR;
  int dir = 0;
  snd_pcm_hw_params_set_rate_near(g_pcm_cap, hw, &rate, &dir);
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
  logf_("采集 %uch@%uHz period=%u buffer=%u", ch, rate, (unsigned)period, (unsigned)buffer);
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
  unsigned rate = SR;
  int dir = 0;
  snd_pcm_hw_params_set_rate_near(g_pcm_play, hw, &rate, &dir);
  snd_pcm_uframes_t period = FRAME_SAMPLES;
  snd_pcm_uframes_t buffer = FRAME_SAMPLES * 8;
  snd_pcm_hw_params_set_period_size_near(g_pcm_play, hw, &period, &dir);
  snd_pcm_hw_params_set_buffer_size_near(g_pcm_play, hw, &buffer);
  rc = snd_pcm_hw_params(g_pcm_play, hw);
  if (rc < 0) {
    logf_("播放参数失败: %s", snd_strerror(rc));
    return -1;
  }
  snd_pcm_prepare(g_pcm_play);
  logf_("播放 %uch@%uHz period=%u buffer=%u", ch, rate, (unsigned)period, (unsigned)buffer);
  return 0;
}

// --------------------------------------------------------------------------- main

static void usage(const char *p) {
  fprintf(stderr,
          "用法: %s [选项]\n"
          "  -c N   固定麦克风通道 0-5（默认自适应选路）\n"
          "  -a     ch0-5 六路平均后单通道 AEC（省 CPU）\n"
          "  -g X   麦克风数字增益（默认 56，约 +35dB，说话目标 -18dBFS）\n"
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
      case 'c': g_select_mode = 1; g_fixed_ch = atoi(optarg); break;
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

  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  signal(SIGUSR1, on_usr1);
  signal(SIGUSR2, on_usr2);
  signal(SIGPIPE, SIG_IGN);


  if (ring_init(&g_cap_ring, FRAME_SAMPLES * CAP_CH * 8) < 0) return 1;
  if (ring_init(&g_up_ring, FRAME_SAMPLES * 16) < 0) return 1;
  if (ring_init(&g_play_ring, FRAME_SAMPLES * PLAY_RING_FRAMES) < 0) return 1;

  if (g_aec_on) {
    g_echo = speex_echo_state_init(FRAME_SAMPLES, AEC_FILTER_LEN);
    if (!g_echo) {
      logf_("AEC 初始化失败");
      return 1;
    }
    int sr = SR;
    speex_echo_ctl(g_echo, SPEEX_ECHO_SET_SAMPLING_RATE, &sr);
  }

  if (g_ns_on) {
    g_pp = speex_preprocess_state_init(FRAME_SAMPLES, SR);
    if (g_pp) {
      int one = 1, zero = 0;
      speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_DENOISE, &one);
      speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_VAD, &zero);
      /* 固定增益已把说话抬到 -18dBFS；降噪再压约 20dB 噪声底，避免远场底噪被听见 */
      {
        int ns = -20;
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_NOISE_SUPPRESS, &ns);
      }
      // AGC 默认关：大固定增益 + AGC 容易把噪声底也爬起来。需要时用 -A。
      int agc = g_agc_on ? 1 : 0;
      speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC, &agc);
      if (g_agc_on) {
        int target = 12000; /* 约 -8.7 dBFS，留余量防削顶 */
        int inc = 12, dec = 6;
        int maxgain = 16384;
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC_TARGET, &target);
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC_INCREMENT, &inc);
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC_DECREMENT, &dec);
        speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_AGC_MAX_GAIN, &maxgain);
      }
      if (g_aec_on && g_echo) speex_preprocess_ctl(g_pp, SPEEX_PREPROCESS_SET_ECHO_STATE, g_echo);
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

  logf_("就绪 up=%d down=%d AEC=%d NS=%d 选路=%d gain=%.2f post=%ddB", UP_PORT, DOWN_PORT,
        g_aec_on, g_ns_on, g_select_mode, g_gain_mic, g_gain_post_db);

  while (!g_stop) {
    for (int i = 0; i < 30 && !g_stop; i++) {
      if (g_do_probe) {
        g_do_probe = 0;
        probe_channels();
      }
      sleep(1);
    }
    if (!g_stop) dump_stats();
  }

  logf_("退出中…");
  close(lfd_up);
  close(lfd_down);
  pthread_join(t1, NULL);
  pthread_join(t2, NULL);
  pthread_join(t3, NULL);
  pthread_join(t4, NULL);
  pthread_join(t5, NULL);

  if (g_pcm_cap) snd_pcm_close(g_pcm_cap);
  if (g_pcm_play) snd_pcm_close(g_pcm_play);
  if (g_echo) speex_echo_state_destroy(g_echo);
  if (g_pp) speex_preprocess_state_destroy(g_pp);
  ring_free(&g_cap_ring);
  ring_free(&g_up_ring);
  ring_free(&g_play_ring);
  logf_("已退出");
  return 0;
}