# S12 音频守护进程
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -std=gnu99
CFLAGS  += $(shell pkg-config --cflags alsa 2>/dev/null)
LDFLAGS += $(shell pkg-config --libs alsa 2>/dev/null) -lspeexdsp -lm -lpthread

BIN := build/s12audio

all: $(BIN)

$(BIN): s12audio.c
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -rf build

install: $(BIN)
	@echo "用 scp 请走: cat build/s12audio | ssh root@192.168.1.3 'cat > /bin/s12audio && chmod +x /bin/s12audio'"

.PHONY: all clean install