# 交叉编译 volumed（与 s12audio 同代 glibc）
#   docker build -t s12build ../s12audio
#   docker run --rm -v $PWD:/src -w /src s12build make volumed

CC ?= gcc
CFLAGS ?= -O2 -Wall -Wextra -std=gnu99

.PHONY: all volumed clean

all: volumed

volumed: volumed.c
	@mkdir -p build
	$(CC) $(CFLAGS) -o build/volumed $<
	@ls -lh build/volumed

clean:
	rm -rf build
