#pragma once
/* 定长 S16 环形缓冲（单生产者 + 单消费者）。
 *
 * 从 s12audio.c 抽出，目的：
 *   1. 让它可以在 Mac 上离线单测（不需要 ALSA / speex / 设备）；
 *   2. 写入改成最多两段 memcpy，不再逐样本持锁；
 *   3. 每个 ring 自带溢出计数，日志里能分清是 cap/up/play 哪一个在丢帧。
 *
 * 注意：仍然用互斥量保护，不改成无锁。原因是这里的临界区已经是 memcpy
 * 量级（~2µs/帧、100Hz），加锁开销远小于引入 memory barrier 的复杂度与
 * 老 glibc/老内核上的正确性风险。
 */
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct {
  int16_t *buf;
  int cap;
  int rd;
  int wr;
  int count; /* 当前有效样本数；用计数而不是指针差，覆盖时不会算成负数 */
  unsigned long dropped; /* 因写满被挤掉的最旧样本数（per-ring） */
  pthread_mutex_t m;
} ring_t;

static inline int ring_init(ring_t *r, int cap) {
  if (cap <= 0) return -1;
  r->buf = (int16_t *)calloc((size_t)cap, sizeof(int16_t));
  if (!r->buf) return -1;
  r->cap = cap;
  r->rd = r->wr = 0;
  r->count = 0;
  r->dropped = 0;
  return pthread_mutex_init(&r->m, NULL) == 0 ? 0 : -1;
}

static inline void ring_free(ring_t *r) {
  free(r->buf);
  r->buf = NULL;
  pthread_mutex_destroy(&r->m);
}

static inline void ring_clear(ring_t *r) {
  pthread_mutex_lock(&r->m);
  r->rd = r->wr = 0;
  r->count = 0;
  pthread_mutex_unlock(&r->m);
}

static inline int ring_avail(ring_t *r) {
  pthread_mutex_lock(&r->m);
  int n = r->count;
  pthread_mutex_unlock(&r->m);
  return n;
}

static inline void ring_write(ring_t *r, const int16_t *x, int n) {
  if (n <= 0) return;
  pthread_mutex_lock(&r->m);

  /* 一次写入超过容量：只有最后 cap 个样本能存活，直接跳过前面的，
     避免下面的环回填充逻辑在 n > cap 时写超过一圈并把 wr 算错。 */
  if (n > r->cap) {
    r->dropped += (unsigned long)(r->count + n - r->cap);
    x += n - r->cap;
    n = r->cap;
    r->rd = r->wr;
    r->count = 0;
  }

  /* 写满时丢最旧：一次性跳过 drop 个样本，而不是逐样本判断 */
  int drop = r->count + n - r->cap;
  if (drop > 0) {
    r->rd = (r->rd + drop) % r->cap;
    r->count -= drop;
    r->dropped += (unsigned long)drop;
  }

  /* 最多两段：写到 buffer 尾部 + 绕回开头 */
  int first = r->cap - r->wr;
  if (first > n) first = n;
  memcpy(r->buf + r->wr, x, (size_t)first * sizeof(int16_t));
  r->wr = (r->wr + first) % r->cap;
  if (n > first) {
    memcpy(r->buf, x + first, (size_t)(n - first) * sizeof(int16_t));
    r->wr = n - first;
  }
  r->count += n;

  pthread_mutex_unlock(&r->m);
}

/* 读满 n 个样本；超时返回实际读到的。
 * 刻意不用条件变量：帧率只有 100Hz（10ms），2ms 级轮询开销可忽略，
 * 但能彻底避免「丢失唤醒 → 永久阻塞」这类只在老 glibc 上出现的坑。 */
static inline int ring_read(ring_t *r, int16_t *x, int n, int timeout_ms) {
  int deadline_us = timeout_ms * 1000;
  int waited = 0;
  int got = 0;
  while (got < n) {
    pthread_mutex_lock(&r->m);
    int avail = r->count;
    if (avail > 0) {
      int take = n - got;
      if (take > avail) take = avail;
      int first = r->cap - r->rd;
      if (first > take) first = take;
      memcpy(x + got, r->buf + r->rd, (size_t)first * sizeof(int16_t));
      if (take > first) {
        memcpy(x + got + first, r->buf, (size_t)(take - first) * sizeof(int16_t));
      }
      r->rd = (r->rd + take) % r->cap;
      r->count -= take;
      got += take;
    }
    pthread_mutex_unlock(&r->m);

    if (got >= n) break;
    if (waited >= deadline_us) break;
    usleep(2000);
    waited += 2000;
  }
  return got;
}