# tools/common 的离线单测：不依赖 ALSA / speex / 设备
#
# 这几个头文件是 s12audio 与 volumed 的共享内核，能在宿主机（macOS/Linux）
# 直接编译，所以环形缓冲、帧协议、增益曲线、状态文件这些逻辑
# 不需要「scp 上设备靠听」才能验证。
#
#   make test
#
# 注意用宿主机编译器，不要用 s12build 容器（那是 aarch64 交叉工具链）。
CC     ?= cc
CFLAGS ?= -O1 -g -Wall -Wextra -std=gnu11

.PHONY: test clean

test: test_common
	./test_common

test_common: test_common.c gain.h ring.h proto.h state_file.h
	$(CC) $(CFLAGS) -o $@ test_common.c -lpthread -lm

clean:
	rm -f test_common
