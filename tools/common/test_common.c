/* tools/common 的离线单测：不依赖 ALSA / speex / 设备。
 *
 *   make test        （在 tools/s12audio 下）
 *   cc -o t test_common.c && ./t
 *
 * 覆盖 {ring, proto, gain, state_file} 四块的边界条件；这些正是过去只能
 * 「scp 上设备靠听」才能发现的地方。
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "gain.h"
#include "proto.h"
#include "ring.h"
#include "state_file.h"

static int g_fail = 0;
static int g_pass = 0;

#define CHECK(cond, fmt, ...)                        \
  do {                                               \
    if (cond) {                                      \
      g_pass++;                                      \
    } else {                                         \
      g_fail++;                                      \
      printf("  FAIL %s:%d " fmt "\n", __FILE__, __LINE__, ##__VA_ARGS__); \
    }                                                \
  } while (0)

// --------------------------------------------------------------------------- ring

static void test_ring_basic(void) {
  ring_t r;
  CHECK(ring_init(&r, 8) == 0, "init");
  int16_t in[5] = {1, 2, 3, 4, 5};
  ring_write(&r, in, 5);
  CHECK(ring_avail(&r) == 5, "avail=%d", ring_avail(&r));
  int16_t out[5] = {0};
  CHECK(ring_read(&r, out, 5, 10) == 5, "read 5");
  CHECK(memcmp(in, out, sizeof(in)) == 0, "roundtrip");
  CHECK(ring_avail(&r) == 0, "empty after read");
  CHECK(r.dropped == 0, "no drops");
  ring_free(&r);
}

/* 绕回：写 6、读 6、再写 6 —— 第二次写入必然跨过 buffer 尾部 */
static void test_ring_wrap(void) {
  ring_t r;
  ring_init(&r, 8);
  int16_t a[6] = {1, 2, 3, 4, 5, 6};
  int16_t b[6] = {7, 8, 9, 10, 11, 12};
  int16_t got[6];
  ring_write(&r, a, 6);
  ring_read(&r, got, 6, 10);
  ring_write(&r, b, 6);
  CHECK(ring_avail(&r) == 6, "avail");
  ring_read(&r, got, 6, 10);
  CHECK(memcmp(got, b, sizeof(b)) == 0, "wrap data");
  CHECK(r.dropped == 0, "wrap must not drop");
  ring_free(&r);
}

/* 写满：丢的是**最旧**的，且 dropped 计数精确 */
static void test_ring_overflow(void) {
  ring_t r;
  ring_init(&r, 4);
  int16_t a[3] = {1, 2, 3};
  int16_t b[3] = {4, 5, 6};
  int16_t got[4];
  ring_write(&r, a, 3);
  ring_write(&r, b, 3); /* 需要 6，容量 4 → 丢最旧的 2 个 (1,2) */
  CHECK(r.dropped == 2, "dropped=%lu", r.dropped);
  CHECK(ring_avail(&r) == 4, "avail=%d", ring_avail(&r));
  ring_read(&r, got, 4, 10);
  CHECK(got[0] == 3 && got[1] == 4 && got[2] == 5 && got[3] == 6,
        "kept newest 4: %d %d %d %d", got[0], got[1], got[2], got[3]);
  ring_free(&r);
}

/* 一次写入远大于容量：只保留尾部 cap 个样本 */
static void test_ring_overwrite_whole(void) {
  ring_t r;
  ring_init(&r, 4);
  int16_t a[10];
  for (int i = 0; i < 10; i++) a[i] = (int16_t)i;
  ring_write(&r, a, 10); /* 丢 6 个，留 4,5,6,7 */
  int16_t got[4];
  ring_read(&r, got, 4, 10);
  CHECK(got[0] == 6 && got[3] == 9, "tail kept: %d..%d", got[0], got[3]);
  ring_free(&r);
}

static void test_ring_timeout(void) {
  ring_t r;
  ring_init(&r, 16);
  int16_t got[4];
  /* 空 ring：应当等到超时后返回 0，而不是永久阻塞 */
  CHECK(ring_read(&r, got, 4, 20) == 0, "timeout returns 0");
  ring_free(&r);
}

/* 单生产者 + 单消费者并发：总量守恒（丢的 + 收的 == 发的） */
typedef struct {
  ring_t *r;
  int n;
  volatile int done;
} prod_arg_t;

static void *producer(void *p) {
  prod_arg_t *a = (prod_arg_t *)p;
  for (int i = 0; i < a->n; i++) {
    int16_t v = (int16_t)(i & 0x7FFF);
    ring_write(a->r, &v, 1);
    usleep(50); /* 让消费者跟上；本测试验证的是守恒，不是吞吐 */
  }
  a->done = 1;
  return NULL;
}

static void test_ring_threads(void) {
  enum { N = 2000 };
  ring_t r;
  ring_init(&r, 64);
  prod_arg_t a = {&r, N, 0};
  pthread_t t;
  pthread_create(&t, NULL, producer, &a);
  long consumed = 0;
  int16_t v;
  while (!a.done || ring_avail(&r) > 0) {
    consumed += ring_read(&r, &v, 1, 5);
  }
  pthread_join(t, NULL);
  CHECK(consumed + (long)r.dropped == N, "conserved consumed=%ld dropped=%lu",
        consumed, r.dropped);
  ring_free(&r);
}

// --------------------------------------------------------------------------- proto

static void test_proto_hdr(void) {
  frame_hdr_t h;
  hdr_pack(&h, F_VOLUME, 7, 12345);
  CHECK(h.type == F_VOLUME, "type");
  CHECK(h.flags == 7, "flags");
  CHECK(h.reserved == 0, "reserved");
  CHECK(hdr_get_length(&h) == 12345, "length");
  /* 布局必须与 Python struct.Struct("<BBHI") 一致 = 8 字节 */
  CHECK(sizeof(frame_hdr_t) == 8, "hdr size=%zu", sizeof(frame_hdr_t));
}

/* 对端分多次小包写：io_read_full 必须拼满 */
static void test_io_read_partial(void) {
  int sv[2];
  CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0, "socketpair");
  const char *msg = "hello-world-1234";
  size_t len = strlen(msg);
  for (size_t i = 0; i < len; i++) {
    ssize_t n = write(sv[0], msg + i, 1); /* 一个字节一个字节地喂 */
    CHECK(n == 1, "byte write");
  }
  char buf[32] = {0};
  io_result_t r = io_read_full(sv[1], buf, len, NULL, 200);
  CHECK(r == IO_OK, "read result=%d", r);
  CHECK(memcmp(buf, msg, len) == 0, "payload");
  close(sv[0]);
  close(sv[1]);
}

/* 对端不写：必须超时返回，不能挂死 */
static void test_io_read_timeout(void) {
  int sv[2];
  socketpair(AF_UNIX, SOCK_STREAM, 0, sv);
  char buf[8];
  io_result_t r = io_read_full(sv[1], buf, sizeof(buf), NULL, 60);
  CHECK(r == IO_TIMEOUT, "timeout result=%d", r);
  close(sv[0]);
  close(sv[1]);
}

/* stop 置位后必须立刻返回 IO_STOPPED（SIGTERM 能穿透 recv 的前提） */
static void test_io_read_stop(void) {
  int sv[2];
  socketpair(AF_UNIX, SOCK_STREAM, 0, sv);
  volatile sig_atomic_t stop = 1;
  char buf[8];
  io_result_t r = io_read_full(sv[1], buf, sizeof(buf), &stop, 500);
  CHECK(r == IO_STOPPED, "stop result=%d", r);
  close(sv[0]);
  close(sv[1]);
}

/* 对端关闭 */
static void test_io_read_closed(void) {
  int sv[2];
  socketpair(AF_UNIX, SOCK_STREAM, 0, sv);
  close(sv[0]);
  char buf[8];
  io_result_t r = io_read_full(sv[1], buf, sizeof(buf), NULL, 200);
  CHECK(r == IO_CLOSED, "closed result=%d", r);
  close(sv[1]);
}

// --------------------------------------------------------------------------- gain

static void test_gain(void) {
  double prev = -1;
  for (int p = JARVIS_VOL_MIN_PCT; p <= JARVIS_VOL_MAX_PCT; p++) {
    double g = jarvis_pct_gain(p);
    CHECK(g > prev, "monotonic at %d", p);
    prev = g;
  }
  CHECK(jarvis_vol_clamp(0) == JARVIS_VOL_MIN_PCT, "clamp low");
  CHECK(jarvis_vol_clamp(101) == 100, "clamp high");
  CHECK(jarvis_vol_clamp(-5) == JARVIS_VOL_MIN_PCT, "clamp negative");
  CHECK(jarvis_pct_gain(100) == 1.0, "100%% = unity");
  CHECK(jarvis_pct_gain(50) > 0.49 && jarvis_pct_gain(50) < 0.51, "50%% linear");
  /* 灯弧 */
  CHECK(jarvis_vol_led_count(100) == 12, "led 100");
  CHECK(jarvis_vol_led_count(50) == 6, "led 50");
  CHECK(jarvis_vol_led_count(0) >= 1, "led min 1");
  CHECK(jarvis_vol_led_count(10) == 1, "led 10 -> 1");
  /* dB 与线性一致 */
  double db = jarvis_pct_db(100);
  CHECK(db > -0.001 && db < 0.001, "db(100)=%f", db);
  CHECK(jarvis_sfx_amp(100) > jarvis_sfx_amp(5), "sfx increases");

  /* wire byte：与 jarvis/volume.py::percent_to_gain_byte 必须逐点一致。
     参考实现 = max(0, min(60, 60 + round(20*log10(p/100))))，Python 的
     round() 对 .5 走 banker's rounding，所以这里用整数算式复刻。 */
  CHECK(jarvis_pct_to_byte(100) == 60, "byte 100 -> 60, got %d", jarvis_pct_to_byte(100));
  CHECK(jarvis_pct_to_byte(50) == 54, "byte 50 -> 54, got %d", jarvis_pct_to_byte(50));
  CHECK(jarvis_pct_to_byte(5) == 34, "byte 5 -> 34, got %d", jarvis_pct_to_byte(5));
  /* 往返：byte → pct → byte 应当回到同一档。注意 pct 有 5% 下限，
     byte 34 以下全部塌到 pct=5，所以只对下限以上的区间要求无损。 */
  for (int b = 34; b <= 60; b++) {
    int p = jarvis_byte_to_pct(b);
    int b2 = jarvis_pct_to_byte(p);
    CHECK(abs(b2 - b) <= 1, "roundtrip byte %d -> pct %d -> byte %d", b, p, b2);
  }
  /* 下限以下必须一致塌到最小音量，而不是产生更低的 pct */
  for (int b = 0; b < 34; b++) {
    CHECK(jarvis_byte_to_pct(b) == JARVIS_VOL_MIN_PCT, "byte %d -> floor", b);
  }
  CHECK(jarvis_byte_to_pct(60) == 100, "byte 60 -> 100");
  CHECK(jarvis_byte_to_pct(0) <= JARVIS_VOL_MIN_PCT, "byte 0 -> floor");
}

// --------------------------------------------------------------------------- state_file

static void test_state_file(void) {
  const char *path = "/tmp/jarvis_selftest_vol.pct";
  unlink(path);
  CHECK(jarvis_read_int(path, -1) == -1, "missing -> default");
  CHECK(jarvis_write_int(path, 42) == 0, "write");
  CHECK(jarvis_read_int(path, -1) == 42, "read back");
  CHECK(jarvis_write_int(path, 7) == 0, "overwrite");
  CHECK(jarvis_read_int(path, -1) == 7, "read back 2");
  /* 原子写不应留下临时文件 */
  char tmp[256];
  snprintf(tmp, sizeof(tmp), "%s.tmp.%d", path, (int)getpid());
  CHECK(access(tmp, F_OK) != 0, "no leftover tmp");
  /* 非法内容回退到默认值 */
  FILE *f = fopen(path, "w");
  fprintf(f, "not-a-number\n");
  fclose(f);
  CHECK(jarvis_read_int(path, -9) == -9, "garbage -> default");
  unlink(path);
}

int main(void) {
  printf("ring:\n");
  test_ring_basic();
  test_ring_wrap();
  test_ring_overflow();
  test_ring_overwrite_whole();
  test_ring_timeout();
  test_ring_threads();
  printf("proto:\n");
  test_proto_hdr();
  test_io_read_partial();
  test_io_read_timeout();
  test_io_read_stop();
  test_io_read_closed();
  printf("gain:\n");
  test_gain();
  printf("state_file:\n");
  test_state_file();

  printf("\n%d passed, %d failed\n", g_pass, g_fail);
  return g_fail == 0 ? 0 : 1;
}