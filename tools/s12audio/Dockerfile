# S12 音频守护进程交叉编译环境
#
# 设备：aarch64 / EGLIBC 2.19 / Linux 3.14.29
# 策略：用与设备同时代的 debian:jessie arm64（glibc 2.19）原生编译，
#       避免现代工具链引入 GLIBC_2.2x+ 符号导致设备跑不起来（设备只有 2.19）。
#
# 用法（注意挂载的是 tools/，不是 tools/s12audio/）：
#   docker build -t s12build tools/s12audio
#   docker run --rm -v $PWD/tools:/src/tools -w /src/tools/s12audio s12build
#
# 为什么要挂整个 tools/：s12audio 现在依赖 ../common/{gain,ring,proto,state_file}.h
# 四个可离线单测的小模块（AEC 调参逻辑已从 s12audio.c 里拆出去）。
# 只挂 tools/s12audio 会找不到 gain.h —— 这是本文件上一次的坑。
#
# 产物：tools/s12audio/build/s12audio
#
# 关于 -static：设备上没有 libasound.a / libspeexdsp.a，全静态链接会在
#   「/usr/bin/ld: cannot find -lasound」处失败（已实测）。因此产物是
#   **动态链接** + strip（Makefile 里 LDFLAGS += -s）。本注释此前写「要求静态
#   二进制」，与事实不符，已更正。

FROM arm64v8/debian:jessie

# jessie 已 EOL：换 archive 源、跳过过期签名校验、必须 --allow-unauthenticated
# 注意：老版 apt 不认识 --allow-insecure-repositories，update 不能带它，
# 只靠 apt.conf 里关掉 Check-Valid-Until；install 阶段才用 --allow-unauthenticated。
RUN printf 'deb http://archive.debian.org/debian jessie main\n' > /etc/apt/sources.list \
    && printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99archive \
    && apt-get update 2>&1 | tail -2 \
    && (apt-get install -y --no-install-recommends --allow-unauthenticated \
        build-essential \
        libasound2-dev \
        libspeexdsp-dev \
        pkg-config \
     || apt-get install -y --no-install-recommends --allow-unauthenticated --fix-missing \
        build-essential \
        libasound2-dev \
        libspeexdsp-dev \
     || apt-get install -y --no-install-recommends --allow-unauthenticated --fix-missing \
        build-essential \
        libasound2-dev \
        libspeexdsp-dev) \
    && dpkg -s libasound2-dev libspeexdsp-dev >/dev/null \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/tools/s12audio
CMD ["sh", "-c", "make clean && make && ls -lh build/"]