ARG APP_BASE=mediafarm/app-base:worker-dev
ARG MEDIA_BASE=mediafarm/media-base:cpu-dev

FROM ${MEDIA_BASE} AS media
FROM ${APP_BASE}
USER root
COPY --from=media /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
                  /usr/local/bin/ffmbc /usr/local/bin/yt-dlp /usr/local/bin/
COPY --from=media /usr/lib/x86_64-linux-gnu/libx264.so.* /usr/lib/x86_64-linux-gnu/
COPY --from=media /usr/lib/x86_64-linux-gnu/libmp3lame.so.* /usr/lib/x86_64-linux-gnu/
RUN apt-get update && apt-get install -y --no-install-recommends libnuma1 libxml2 \
 && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -version && ffprobe -version && yt-dlp --version && ffmbc -version
USER 10001
ENV MF_ROLE=worker MF_SCRATCH=/scratch MF_METRICS_PORT=9100
EXPOSE 9100
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -m worker.healthcheck
CMD ["python", "-m", "worker"]