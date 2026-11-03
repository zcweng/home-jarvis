/* S12 音量环 + 麦静音键守护：独占 /dev/input/event0。
 *
 * 音量环 → 0–100% 灯弧 + UDP "V<pct>\n"
 * 麦键（原厂 code 106）→ 切换静音，黄呼吸灯 + UDP "M1\\n"/"M0\\n"
 * Mac IP 读 /tmp/jarvis-mac.ip（Jarvis 启动时写入）
 *
 * 编译见 tools/s12led/Makefile
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#define EVENT_DEV "/dev/input/event0"
#define PCT_FILE "/tmp/jarvis-vol.pct"
#define MUTE_FILE "/tmp/jarvis-mic.mute"
#define MAC_IP_FILE "/tmp/jarvis-mac.ip"
#define LED_SET "/data/jarvis-led-set.sh"
#define UDP_PORT 8769
#define VOL_MIN_PCT 5
/* 原厂 touchpad：106=mute（KEY_RIGHT 名），兼听标准 MUTE/MICMUTE */
#define KEY_S12_MUTE 106
#ifndef KEY_MICMUTE
#define KEY_MICMUTE 248
#endif

static volatile int g_stop = 0;
static int g_pct = 70;
static int g_muted = 0;
static int g_last_abs = -1;

static void on_sig(int s) {
  (void)s;
  g_stop = 1;
}

static void load_pct(void) {
  FILE *f = fopen(PCT_FILE, "r");
  if (!f) return;
  int v = 70;
  if (fscanf(f, "%d", &v) == 1) {
    if (v < VOL_MIN_PCT) v = VOL_MIN_PCT;
    if (v > 100) v = 100;
    g_pct = v;
  }
  fclose(f);
}

static void udp_send(const char *msg, size_t len) {
  char ip[64] = {0};
  FILE *f = fopen(MAC_IP_FILE, "r");
  if (f) {
    if (fgets(ip, sizeof(ip), f)) {
      char *nl = strchr(ip, '\n');
      if (nl) *nl = 0;
    }
    fclose(f);
  }
  if (!ip[0]) return;
  int fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (fd < 0) return;
  struct sockaddr_in addr;
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons(UDP_PORT);
  if (inet_aton(ip, &addr.sin_addr)) {
    sendto(fd, msg, len, 0, (struct sockaddr *)&addr, sizeof(addr));
  }
  close(fd);
}

static void load_mute(void) {
  FILE *f = fopen(MUTE_FILE, "r");
  if (!f) {
    g_muted = 0;
    return;
  }
  int v = 0;
  if (fscanf(f, "%d", &v) == 1) g_muted = v ? 1 : 0;
  fclose(f);
}

static void apply_mute(int muted) {
  g_muted = muted ? 1 : 0;
  {
    FILE *f = fopen(MUTE_FILE, "w");
    if (f) {
      fprintf(f, "%d\n", g_muted);
      fclose(f);
    }
  }
  char cmd[128];
  snprintf(cmd, sizeof(cmd), "%s %s", LED_SET, g_muted ? "mute" : "unmute");
  int st = system(cmd);
  (void)st;
  char msg[8];
  int n = snprintf(msg, sizeof(msg), "M%d\n", g_muted);
  udp_send(msg, (size_t)n);
  fprintf(stderr, "volumed: mic %s\n", g_muted ? "MUTE" : "ON");
}

static void toggle_mute(void) { apply_mute(!g_muted); }

static void apply_force(int pct, int force) {
  if (pct < VOL_MIN_PCT) pct = VOL_MIN_PCT;
  if (pct > 100) pct = 100;
  if (!force && pct == g_pct) return;
  g_pct = pct;

  char cmd[128];
  snprintf(cmd, sizeof(cmd), "%s volume %d", LED_SET, g_pct);
  int st = system(cmd);
  (void)st;

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

int main(void) {
  signal(SIGINT, on_sig);
  signal(SIGTERM, on_sig);
  /* 忽略 HUP，SSH 拉起后不断 */
  signal(SIGHUP, SIG_IGN);

  load_pct();
  load_mute();
  if (g_muted) {
    apply_mute(1); /* 恢复黄灯 */
  } else {
    apply_force(g_pct, 1); /* 开机强制同步灯弧 */
  }

  int fd = open(EVENT_DEV, O_RDONLY);
  if (fd < 0) {
    perror("open event0");
    return 1;
  }
  /* 尝试独占，避免与 touchpad 抢 */
  if (ioctl(fd, EVIOCGRAB, 1) < 0) {
    fprintf(stderr, "volumed: EVIOCGRAB failed (%s), continue shared\n", strerror(errno));
  }

  fprintf(stderr, "volumed: listening %s, pct=%d mute=%d\n", EVENT_DEV, g_pct, g_muted);

  struct input_event ev;
  while (!g_stop) {
    ssize_t n = read(fd, &ev, sizeof(ev));
    if (n < 0) {
      if (errno == EINTR) continue;
      break;
    }
    if ((size_t)n < sizeof(ev)) continue;

    if (ev.type == EV_KEY && ev.value == 1) {
      if (is_mute_key(ev.code)) toggle_mute();
      else if (ev.code == KEY_VOLUMEUP || ev.code == KEY_UP) step(5);
      else if (ev.code == KEY_VOLUMEDOWN || ev.code == KEY_DOWN) step(-5);
    } else if (ev.type == EV_ABS) {
      /* 环形滑动：ABS_X/ABS_WHEEL/ABS_MISC 等，用相对变化步进 */
      int code = ev.code;
      int val = ev.value;
      if (code == ABS_X || code == ABS_Y || code == ABS_WHEEL || code == ABS_MISC ||
          code == ABS_Z || code == ABS_RX) {
        if (g_last_abs < 0) {
          g_last_abs = val;
          continue;
        }
        int d = val - g_last_abs;
        /* 处理回绕（若值域 0..N） */
        if (d > 50) d -= 100;
        if (d < -50) d += 100;
        g_last_abs = val;
        if (d >= 2) step(3);
        else if (d <= -2) step(-3);
      }
    } else if (ev.type == EV_REL) {
      if (ev.code == REL_WHEEL || ev.code == REL_HWHEEL || ev.code == REL_X) {
        if (ev.value > 0) step(4);
        else if (ev.value < 0) step(-4);
      }
    }
  }

  ioctl(fd, EVIOCGRAB, 0);
  close(fd);
  return 0;
}
