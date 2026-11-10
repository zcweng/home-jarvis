# beam 模块的宿主端单测（Mac 直接跑，不经设备）
#
# 目的：设备上那套延迟求和实现必须和 scripts/s12_beam_eval.py 里验证过的
# numpy 原型**同源**。这里用同一段录音跑出 mono，再用 python 比波形/SNR。
CC     ?= cc
CFLAGS ?= -O2 -Wall -Wextra -std=gnu99
SRC    := beamtest.c ../s12audio/beam.c
BIN    := build/beamtest

all: $(BIN)

$(BIN): $(SRC) ../s12audio/beam.h
	@mkdir -p build
	$(CC) $(CFLAGS) -I../s12audio -o $@ $(SRC) -lm

clean:
	rm -rf build

.PHONY: all clean