#pragma once
/* 下行帧协议（Mac → 设备）：8 字节头 + payload。唯一实现。
 *
 * 之前帧解析内联在 downlink_thread 里，边界条件（length=0、length 非法、
 * 短读、EINTR）只能靠上设备试。抽出来后可以对着 socketpair 单测。
 *
 * 与 jarvis/endpoint.py 的 HDR = struct.Struct("<BBHI") 严格一致。
 */
#include <arpa/inet.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define F_PCM 1
#define F_FLUSH 2
#define F_VOLUME 3
#define F_PING 4

#define FRAME_HDR_SIZE 8
#define PROTO_MAX_PAYLOAD (1024 * 1024)

typedef struct __attribute__((packed)) {
  uint8_t type;
  uint8_t flags;
  uint16_t reserved;
  uint32_t length;
} frame_hdr_t;

/* 可中断的读：distinguish 超时 / 对端关闭 / 出错 / 收到停止信号。
 * 之前的写法是裸 recv 且没设 SO_RCVTIMEO，对端半死时 SIGTERM 也唤不醒，
 * pthread_join 永久挂住。这里用 poll 带超时，并且每轮检查 *stop。 */
typedef enum {
  IO_OK = 0,
  IO_TIMEOUT = 1,
  IO_CLOSED = 2,
  IO_ERROR = 3,
  IO_STOPPED = 4,
} io_result_t;

static inline io_result_t io_read_full(int fd, void *buf, size_t len,
                                      volatile sig_atomic_t *stop,
                                      int idle_timeout_ms) {
  char *p = (char *)buf;
  size_t got = 0;
  while (got < len) {
    if (stop && *stop) return IO_STOPPED;
    struct pollfd pfd;
    pfd.fd = fd;
    pfd.events = POLLIN;
    pfd.revents = 0;
    int pr = poll(&pfd, 1, idle_timeout_ms);
    if (pr == 0) return IO_TIMEOUT;
    if (pr < 0) {
      if (errno == EINTR) continue;
      return IO_ERROR;
    }
    ssize_t n = recv(fd, p + got, len - got, 0);
    if (n == 0) return IO_CLOSED;
    if (n < 0) {
      if (errno == EINTR) continue;
      if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
      return IO_ERROR;
    }
    got += (size_t)n;
  }
  return IO_OK;
}

static inline uint32_t hdr_get_length(const frame_hdr_t *h) {
  return (uint32_t)h->length; /* 设备与 Mac 同为小端，保持与 Python <I 一致 */
}

static inline void hdr_pack(frame_hdr_t *h, uint8_t type, uint8_t flags,
                            uint32_t length) {
  h->type = type;
  h->flags = flags;
  h->reserved = 0;
  h->length = length;
}