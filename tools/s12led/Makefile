# 交叉编译 volumed（与 s12audio 同代 glibc）
#   docker build -t s12build ../s12audio
#   docker run --rm -v $PWD:/src -w /src s12build make volumed
CC ?= gcc
CFLAGS ?= -O2 -Wall -Wextra -std=gnu99
CFLAGS += -I../common
# 只 strip：设备上 libc 是 2.19 动态的，全静态没有收益且和 s12audio 不一致。
LDFLAGS += -s

.PHONY: all volumed clean

all: volumed

volumed: volumed.c ../common/gain.h ../common/state_file.h
	@mkdir -p build
	$(CC) $(CFLAGS) -o build/volumed volumed.c $(LDFLAGS)
	@ls -lh build/volumed

clean:
	rm -rf build