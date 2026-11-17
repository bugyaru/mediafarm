# syntax=docker/dockerfile:1.7
ARG INSTALL_FFMBC=1

# ---- ffmbc: legacy-тулчейн, отдельный stage ----
FROM ubuntu:18.04 AS ffmbc-build
ARG INSTALL_FFMBC
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git pkg-config yasm libx264-dev libmp3lame-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/bcoudurier/ffmbc.git /src/ffmbc
WORKDIR /src/ffmbc
RUN if [ "$INSTALL_FFMBC" = "1" ]; then \
      ./configure --enable-gpl --enable-libx264 --enable-libmp3lame --disable-doc \
        --extra-cflags="-fcommon" && \
      make -j"$(nproc)" && install -m 0755 ffmbc /usr/local/bin/ffmbc && ffmbc -version ; \
    else \
      printf '#!/bin/sh\necho "ffmbc disabled"\nexit 127\n' > /usr/local/bin/ffmbc && \
      chmod +x /usr/local/bin/ffmbc ; \
    fi

# ---- CPU media base ----
FROM debian:bookworm-slim AS cpu
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl xz-utils ca-certificates libnuma1 libxml2 \
    python3 python3-minimal \
&& rm -rf /var/lib/apt/lists/*
ARG FFMPEG_BUILD=ffmpeg-master-latest-linux64-gpl
RUN curl -fsSL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/${FFMPEG_BUILD}.tar.xz" \
    | tar -xJ --strip-components=2 -C /usr/local/bin --wildcards '*/bin/ff*'
RUN curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
      -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp
COPY --from=ffmbc-build /usr/local/bin/ffmbc /usr/local/bin/ffmbc
COPY --from=ffmbc-build /usr/lib/x86_64-linux-gnu/libx264.so.* /usr/lib/x86_64-linux-gnu/
COPY --from=ffmbc-build /usr/lib/x86_64-linux-gnu/libmp3lame.so.* /usr/lib/x86_64-linux-gnu/
RUN ffmpeg -version && ffprobe -version && yt-dlp --version && ffmbc -version

# ---- CUDA media base: тот же ffmpeg, но с проверкой NVENC/CUDA-фильтров ----
FROM cpu AS cuda
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=video,compute,utility
RUN ffmpeg -hide_banner -encoders | grep -q h264_nvenc \
 && ffmpeg -hide_banner -encoders | grep -q hevc_nvenc \
 && ffmpeg -hide_banner -encoders | grep -q av1_nvenc \
 && ffmpeg -hide_banner -filters  | grep -q scale_cuda \
 && ffmpeg -hide_banner -hwaccels | grep -q cuda