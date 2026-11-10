# S12 六麦阵列原始采集器（测量专用，不参与产品路径）
#
# 交叉编译与 s12audio 共用同一个容器镜像 s12build：
#   docker run --rm -v $PWD/tools:/src/tools -w /src/tools/s12cap s12build
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -std=gnu99
CFLAGS  += $(shell pkg-config --cflags alsa 2>/dev/null) -I../common -I.
LDFLAGS += $(shell pkg-config --libs alsa 2>/dev/null) -lm -lpthread
LDFLAGS += -s

BIN := build/s12cap
SRC := s12cap.c

all: $(BIN)

$(BIN): $(SRC)
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $(SRC) $(LDFLAGS)

clean:
	rm -rf build

.PHONY: all clean