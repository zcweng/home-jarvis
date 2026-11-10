# S12 音频守护进程
#
# 模块划分（见 tools/common/）：
#   gain.h       pct ↔ 线性增益 ↔ dB，全仓唯一曲线
#   ring.h       定长 S16 环形缓冲（可离线单测）
#   proto.h      下行帧编解码 + 可中断读
#   state_file.h /tmp 状态文件的唯一读写实现
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -std=gnu99
CFLAGS  += $(shell pkg-config --cflags alsa 2>/dev/null) -I../common -I.
LDFLAGS += $(shell pkg-config --libs alsa 2>/dev/null) -lspeexdsp -lm -lpthread
# 只 strip，不做全静态链接：
#   设备上只有 libasound.so.2 / libspeexdsp.so.1（无 .a），-static 会在
#   「cannot find -lasound」处失败；而且换静态 libasound 会改变 ALSA 的
#   插件/dlopen 行为，风险大于收益。strip 已把 135K → 124K。
LDFLAGS += -s

BIN := build/s12audio
SRC := s12audio.c beam.c
HDR := $(wildcard ../common/*.h) beam.h mute_sfx_pcm.h vol_dong_pcm.h

all: $(BIN)

# 共用头文件在 tools/common，嵌入式设备上没有安装 gcc 的负担；
# 这里把 -I 指过去，头文件本身不复制、不链接，保持单一来源。
$(BIN): $(SRC) $(HDR)
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $(SRC) $(LDFLAGS)

# 纯逻辑单测（ring / proto / gain / state_file）。
# 在宿主机上跑：make -C tools/common test
# ⚠️ 不要在 s12build 容器里跑：那是 aarch64 工具链，产出的是设备二进制，
#    Mac 上执行会报 "Syntax error: ("。容器里只能验证「能编过」。
test:
	$(MAKE) -C ../common test

clean:
	rm -rf build

install: $(BIN)
	@echo "用 scp 请走: cat build/s12audio | ssh root@192.168.1.3 'cat > /bin/s12audio && chmod +x /bin/s12audio'"

.PHONY: all clean install test