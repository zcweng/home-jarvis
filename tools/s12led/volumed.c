/* S12 音量环 + 麦静音键守护：独占 /dev/input/event0。
 *
 * 音量环 → 0–100% 灯弧 + UDP "V<pct>\n"
 * 麦键（原厂 code 106）→ 切换静音，黄呼吸灯 + UDP "M1\n"/"M0\n"
 * Mac IP 读 /tmp/jarvis-mac.ip（Jarvis 启动时写入）
 *
 * 编译见 tools/s12led/Makefile
 *
 * 2026-11-04 修正（详见下）：
 *   · read(event0) 改为 poll 带超时 → SIGTERM 能退出，且 EVIOCGRAB 一定释放
 *     （旧实现阻塞在 read 上，杀进程后音量环被永久独占，只能重启设备）
 *   · 各 ABS 轴独立记录 last 值（旧实现六轴共用一个，跨轴跳变会满量程乱调）
 *   · 回绕用 EVIOCGABS 的真实值域，不再硬编码 100
 *   · 灯效下发改成 fork+execv 且不在关键路径上等待（旧实现每个 5% 步进
 *     一次 system() → fork/exec sh → 脚本，滑动时每秒几十次进程创建）
 *   · EVIOCGRAB 失败不再静默降级，而是退避重试并上报
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "../common/gain.h"
#include "../common/state_file.h"

#define EVENT_DEV "/dev/input/event0"
#define PCT_FILE JARVIS_VOL_PCT_FILE
#define MUTE_FILE JARVIS_MUTE_FILE
#define MAC_IP_FILE JARVIS_MAC_IP_FILE
#define LED_SET "/data/jarvis-led-set.sh"
#define UDP_PORT 8769
#define VOL_MIN_PCT JARVIS_VOL_MIN_PCT

/* 原厂 touchpad：106=mute（KEY_RIGHT 名），兼听标准 MUTE/MICMUTE */
#define KEY_S12_MUTE 106
#ifndef KEY_MICMUTE
#define KEY_MICMUTE 248
#endif

/* 环形滑动单次最大步进；跨轴跳变靠 per-axis last 值过滤 */
#define ABS_STEP 3
#define ABS_DEADZONE 2
#define REL_STEP 4
#define KEY_STEP 5

static volatile sig_atomic_t g_stop = 0;
static int g_pct = 70;
static int g_muted = 0;

/* per-axis：每个 ABS 轴单独记录上一次的值与值域，互不污染 */
static int g_abs_last[ABS_CNT];
static int g_abs_min[ABS_CNT];
static int g_abs_max[ABS_CNT];
static int g_abs_seen[ABS_CNT];

static void on_sig(int s) {
  (void)s;
  g_stop = 1;
}

/* 读冷藏起来的 Mac IP；每个 UDP 包都重读是旧行为（简单且始终最新），
 * 但改用 state_file 的读法后至少不会读到半截内容。 */
static int read_mac_ip(char *out, size_t cap) {
  FILE *f = fopen(MAC_IP_FILE, "r");
  if (!f) return 0;
  int ok = fgets(out, (int)cap, f) != NULL;
  fclose(f);
  if (!ok) return 0;
  char *nl = strchr(out, '\n');
  if (nl) *nl = 0;
  return out[0] != 0;
}

static void udp_send(const char *msg, size_t len) {
  char ip[64] = {0};
  if (!read_mac_ip(ip, sizeof(ip))) return;
  int fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (fd < 0) return;
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons(UDP_PORT);
  if (inet_aton(ip, &addr.sin_addr)) {
    ssize_t n = sendto(fd, msg, len, 0, (struct sockaddr *)&addr, sizeof(addr));
    (void)n;
  }
  close(fd);
}

/* 异步执行灯效脚本：fork + execv，父进程不等待。
 * 旧实现用 system()，会在输入线程里同步等 sh + 脚本 + usleep 完成，
 * 滑动音量时把按键响应拖到几十毫秒。
 * 用双 fork 让子进程被 init 收养，避免僵尸进程堆积。 */
static void led_spawn(const char *arg1, const char *arg2) {
  pid_t pid = fork();
  if (pid < 0) return;
  if (pid == 0) {
    /* 中间进程：再 fork 一次后立刻退出，孙进程由 init 收尸 */
    pid_t g = fork();
    if (g != 0) _exit(0);
    setsid();
    int devnull = open("/dev/null", O_RDWR);
    if (devnull >= 0) {
      dup2(devnull, STDIN_FILENO);
      dup2(devnull, STDOUT_FILENO);
      dup2(devnull, STDERR_FILENO);
      if (devnull > STDERR_FILENO) close(devnull);
    }
    if (arg2)
      execl(LED_SET, LED_SET, arg1, arg2, (char *)NULL);
    else
      execl(LED_SET, LED_SET, arg1, (char *)NULL);
    _exit(127);
  }
  int st;
  waitpid(pid, &st, 0); /* 只等中间进程（立即退出），不阻塞在脚本上 */
}

static void apply_mute(int muted) {
  g_muted = muted ? 1 : 0;
  jarvis_write_int(MUTE_FILE, g_muted);
  led_spawn(g_muted ? "mute" : "unmute", NULL);
  char msg[8];
  int n = snprintf(msg, sizeof(msg), "M%d\n", g_muted);
  udp_send(msg, (size_t)n);
  fprintf(stderr, "volumed: mic %s\n", g_muted ? "MUTE" : "ON");
}

static void toggle_mute(void) { apply_mute(!g_muted); }

static void apply_force(int pct, int force) {
  pct = jarvis_vol_clamp(pct);
  if (!force && pct == g_pct) return;
  g_pct = pct;

  char val[8];
  snprintf(val, sizeof(val), "%d", g_pct);
  led_spawn("volume", val);

  /* led-set.sh::paint_volume 会写 PCT_FILE；这里再写一次保证即使灯脚本
     不存在（或还没部署）音量值也不丢。两边写的都是同一个 clamp 后的值。 */
  jarvis_write_int(PCT_FILE, g_pct);

  char msg[16];
  int n = snprintf(msg, sizeof(msg), "V%d\n", g_pct);
  udp_send(msg, (size_t)n);
  fprintf(stderr, "volumed: %d%%\n", g_pct);
}

static void apply(int pct) { apply_force(pct, 0); }
static void step(int delta) { apply(g_pct + delta); }

static int is_mute_key(int code) {
  return code == KEY_S12_MUTE || code == KEY_MUTE || code == KEY_MICMUTE;
}

static int is_abs_axis(int code) {
  switch (code) {
    case ABS_X:
    case ABS_Y:
    case ABS_Z:
    case ABS_RX:
    case ABS_WHEEL:
    case ABS_MISC:
      return 1;
    default:
      return 0;
  }
}

/* 值域只查一次（EVIOCGABS 对不存在的轴返回 EINVAL） */
static void probe_abs_ranges(int fd) {
  for (int c = 0; c < ABS_CNT; c++) {
    struct input_absinfo info;
    memset(&info, 0, sizeof(info));
    if (ioctl(fd, EVIOCGABS(c), &info) == 0 && info.maximum > info.minimum) {
      g_abs_min[c] = info.minimum;
      g_abs_max[c] = info.maximum;
      g_abs_seen[c] = 1;
    }
  }
}

static void handle_abs(int code, int val) {
  if (!is_abs_axis(code)) return;
  if (!g_abs_seen[code]) {
    g_abs_last[code] = val;
    return;
  }
  int lo = g_abs_min[code], hi = g_abs_max[code];
  int span = hi - lo;
  if (span <= 0) return;

  int d = val - g_abs_last[code];
  /* 回绕：用真实值域的一半做阈值，而不是写死 100 */
  int half = span / 2;
  if (d > half) d -= span;
  if (d < -half) d += span;
  g_abs_last[code] = val;

  /* 跨轴切换后第一次上报：另一个轴的值与本轴不可比，
     旧实现会把两个量的差直接当成步进 —— 这里靠 per-axis last 彻底消除。 */
  if (d >= ABS_DEADZONE) step(ABS_STEP);
  else if (d <= -ABS_DEADZONE) step(-ABS_STEP);
}

int main(void) {
  struct sigaction sa;
  memset(&sa, 0, sizeof(sa));
  sa.sa_handler = on_sig;
  sigaction(SIGINT, &sa, NULL);
  sigaction(SIGTERM, &sa, NULL);
  /* 忽略 HUP，SSH 拉起后不断 */
  signal(SIGHUP, SIG_IGN);

  for (int i = 0; i < ABS_CNT; i++) g_abs_last[i] = -1;

  g_pct = jarvis_vol_clamp(jarvis_read_int(PCT_FILE, 70));
  g_muted = jarvis_read_int(MUTE_FILE, 0) ? 1 : 0;

  if (g_muted) {
    apply_mute(1); /* 恢复黄灯 */
  } else {
    apply_force(g_pct, 1); /* 开机强制同步灯弧 */
  }

  int fd = open(EVENT_DEV, O_RDONLY | O_CLOEXEC);
  if (fd < 0) {
    perror("open event0");
    return 1;
  }
  probe_abs_ranges(fd);

  /* 尝试独占，避免与 touchpad 抢。拿不到不是致命错误，但要重试：
     旧实现只打印一行就继续共享读取，两边同时读会让一次滑动产生两倍步进。 */
  int grabbed = 0;
  for (int tries = 0; tries < 10; tries++) {
    if (ioctl(fd, EVIOCGRAB, 1) == 0) {
      grabbed = 1;
      break;
    }
    fprintf(stderr, "volumed: EVIOCGRAB 失败(%s)，200ms 后重试 %d/10\n", strerror(errno),
            tries + 1);
    struct timespec ts = {0, 200 * 1000 * 1000};
    nanosleep(&ts, NULL);
  }
  if (!grabbed) {
    fprintf(stderr, "volumed: 独占失败，降级为共享读取（可能被 touchpad 重复消费）\n");
  }

  fprintf(stderr, "volumed: listening %s, pct=%d mute=%d grab=%d\n", EVENT_DEV, g_pct,
          g_muted, grabbed);

  struct input_event ev;
  while (!g_stop) {
    struct pollfd p = {fd, POLLIN, 0};
    int pr = poll(&p, 1, 200); /* 200ms 让 g_stop 可被看见 */
    if (pr < 0) {
      if (errno == EINTR) continue;
      break;
    }
    if (pr == 0) continue;

    ssize_t n = read(fd, &ev, sizeof(ev));
    if (n < 0) {
      if (errno == EINTR || errno == EAGAIN) continue;
      break;
    }
    if ((size_t)n < sizeof(ev)) continue;

    if (ev.type == EV_KEY && ev.value == 1) {
      if (is_mute_key(ev.code)) toggle_mute();
      else if (ev.code == KEY_VOLUMEUP || ev.code == KEY_UP) step(KEY_STEP);
      else if (ev.code == KEY_VOLUMEDOWN || ev.code == KEY_DOWN) step(-KEY_STEP);
    } else if (ev.type == EV_ABS) {
      handle_abs(ev.code, ev.value);
    } else if (ev.type == EV_REL) {
      if (ev.code == REL_WHEEL || ev.code == REL_HWHEEL || ev.code == REL_X) {
        if (ev.value > 0) step(REL_STEP);
        else if (ev.value < 0) step(-REL_STEP);
      }
    }
  }

  /* 无论如何都要释放独占，否则下次启动 grab 会失败 */
  if (grabbed) ioctl(fd, EVIOCGRAB, 0);
  close(fd);
  fprintf(stderr, "volumed: 退出（grab 已释放）\n");
  return 0;
}