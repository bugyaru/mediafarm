ARG APP_BASE=mediafarm/app-base:watcher-dev
FROM ${APP_BASE}
EXPOSE 9101
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:9101/healthz || exit 1
CMD ["python", "-m", "watcher"]