#pragma once
/* /tmp 状态文件的唯一约定与唯一实现。
 *
 * 契约（详见 docs/S12-设备状态契约.md）：
 *   | 文件                     | 唯一写者                        | 读者                          |
 *   |--------------------------|---------------------------------|-------------------------------|
 *   | /tmp/jarvis-vol.pct      | s12audio（唯一 owner）          | volumed / led-set.sh          |
 *   | /tmp/jarvis-mic.mute     | volumed（唯一 owner）           | led-set.sh / s12audio(镜像)   |
 *   | /tmp/jarvis-mac.ip       | Mac 侧 app.py（SSH 写入）       | volumed                       |
 *
 * 之前的问题是四进程自由读写同一批文件、没有 owner、格式各异。
 * 这里只提供「读一个整数 / 原子写一个整数」两个动作，保证格式一致
 * 且不会出现读者看到半截内容。
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define JARVIS_VOL_PCT_FILE "/tmp/jarvis-vol.pct"
#define JARVIS_MUTE_FILE "/tmp/jarvis-mic.mute"
#define JARVIS_MAC_IP_FILE "/tmp/jarvis-mac.ip"

/* 读整数；文件不存在或内容非法时返回 dflt（不写日志，调用方决定）。 */
static inline int jarvis_read_int(const char *path, int dflt) {
  FILE *f = fopen(path, "r");
  if (!f) return dflt;
  int v = dflt;
  if (fscanf(f, "%d", &v) != 1) v = dflt;
  fclose(f);
  return v;
}

/* 原子写：先写临时文件再 rename，读者不会看到半截内容。 */
static inline int jarvis_write_int(const char *path, int v) {
  char tmp[256];
  int n = snprintf(tmp, sizeof(tmp), "%s.tmp.%d", path, (int)getpid());
  if (n <= 0 || (size_t)n >= sizeof(tmp)) return -1;
  FILE *f = fopen(tmp, "w");
  if (!f) return -1;
  int ok = fprintf(f, "%d\n", v) > 0;
  if (fclose(f) != 0) ok = 0;
  if (!ok) {
    unlink(tmp);
    return -1;
  }
  if (rename(tmp, path) != 0) {
    unlink(tmp);
    return -1;
  }
  return 0;
}