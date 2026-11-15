ARG APP_BASE=mediafarm/app-base:ui-dev
FROM ${APP_BASE}
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "-m", "ui"]