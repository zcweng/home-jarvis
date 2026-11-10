/* beam 模块的宿主端测试：读 8ch WAV（s12cap 录的），跑 beam_process，
 * 写 mono WAV。用来和设备端实际行为、以及 scripts/s12_beam_eval.py 的
 * numpy 原型输出三方对齐。
 *
 *   make -C tools/beamtest && ./tools/beamtest/build/beamtest /tmp/cap_speech2.wav /tmp/beam_c.wav
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../s12audio/beam.h"

static int rd_u32(FILE *f) {
  unsigned char b[4];
  if (fread(b, 1, 4, f) != 4) return -1;
  return b[0] | (b[1] << 8) | (b[2] << 16) | ((unsigned)b[3] << 24);
}
static int rd_u16(FILE *f) {
  unsigned char b[2];
  if (fread(b, 1, 2, f) != 2) return -1;
  return b[0] | (b[1] << 8);
}

int main(int argc, char **argv) {
  if (argc < 3) {
    fprintf(stderr, "用法: %s <8ch-in.wav> <mono-out.wav> [gain_dB]\n", argv[0]);
    return 2;
  }
  FILE *f = fopen(argv[1], "rb");
  if (!f) { perror(argv[1]); return 1; }
  char riff[4], wave[4];
  if (fread(riff, 1, 4, f) != 4 || fread((void *)&(unsigned){0}, 1, 0, f) != 0) return 1;
  rd_u32(f);
  if (fread(wave, 1, 4, f) != 4) return 1;
  int ch = 0, bits = 0, rate = 0, data_off = -1, data_len = 0;
  while (data_off < 0) {
    char id[4];
    if (fread(id, 1, 4, f) != 4) break;
    int len = rd_u32(f);
    if (!memcmp(id, "fmt ", 4)) {
      rd_u16(f); ch = rd_u16(f); rate = rd_u32(f); rd_u32(f); rd_u16(f); bits = rd_u16(f);
      if (len > 16) fseek(f, len - 16, SEEK_CUR);
    } else if (!memcmp(id, "data", 4)) {
      data_off = (int)ftell(f);
      data_len = len;
    } else {
      fseek(f, len + (len & 1), SEEK_CUR);
    }
  }
  if (data_off < 0 || ch < 2) {
    fprintf(stderr, "解析失败 ch=%d bits=%d rate=%d\n", ch, bits, rate);
    return 1;
  }
  if (bits != 16) {
    fprintf(stderr, "只支持 S16，实际 %d bit\n", bits);
    return 1;
  }
  long frames = data_len / (2 * ch);
  fprintf(stderr, "输入 %dch %dHz %.2fs\n", ch, rate, (double)frames / rate);

  int16_t *buf = malloc((size_t)frames * ch * sizeof(int16_t));
  if (fseek(f, data_off, SEEK_SET) != 0) return 1;
  if (fread(buf, sizeof(int16_t), (size_t)frames * ch, f) != (size_t)frames * ch) return 1;
  fclose(f);

  double gdb = argc > 3 ? atof(argv[3]) : 0.0;
  double g = pow(10.0, gdb / 20.0);

  const int FR = 160;
  beam_t beam;
  beam_init(&beam, ch > BEAM_MAX_CH ? BEAM_MAX_CH : ch, 500);
  int16_t *out = malloc((size_t)frames * sizeof(int16_t));
  int16_t frame[BEAM_MAX_CH][FR];
  const int16_t *ptrs[BEAM_MAX_CH];
  long done = 0;
  while (done + FR <= frames) {
    for (int c = 0; c < beam.n_ch; c++) {
      for (int i = 0; i < FR; i++) {
        double v = buf[(size_t)(done + i) * ch + c] * g;
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        frame[c][i] = (int16_t)v;
      }
      ptrs[c] = frame[c];
    }
    beam_process(&beam, ptrs, FR, out + done);
    done += FR;
  }
  char stats[512];
  beam_stats(&beam, stats, sizeof(stats));
  fprintf(stderr, "beam: %s\n", stats);

  unsigned char hdr[44] = {0};
  memcpy(hdr, "RIFF", 4);
  int dlen = (int)(done * 2);
  int rlen = 36 + dlen;
  memcpy(hdr + 4, &rlen, 4);
  memcpy(hdr + 8, "WAVEfmt ", 8);
  int v = 16;
  memcpy(hdr + 16, &v, 4);
  short one = 1, block_align = 2, bps = 16;
  memcpy(hdr + 20, &one, 2);
  memcpy(hdr + 22, &one, 2);
  memcpy(hdr + 24, &rate, 4);
  int br = rate * 2;
  memcpy(hdr + 28, &br, 4);
  memcpy(hdr + 32, &block_align, 2);
  memcpy(hdr + 34, &bps, 2);
  memcpy(hdr + 36, "data", 4);
  memcpy(hdr + 40, &dlen, 4);
  FILE *o = fopen(argv[2], "wb");
  if (!o) { perror(argv[2]); return 1; }
  fwrite(hdr, 1, 44, o);
  fwrite(out, 2, (size_t)done, o);
  fclose(o);
  fprintf(stderr, "→ %s (%.2fs)\n", argv[2], (double)done / rate);
  free(buf);
  free(out);
  return 0;
}