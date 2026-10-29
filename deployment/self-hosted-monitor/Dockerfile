ARG BASE_API=firecrawl-monitor-base:unconfigured
FROM ${BASE_API}
USER root
COPY apps/api/src/lib/local-artifacts.ts /tmp/monitor-src/lib/local-artifacts.ts
COPY apps/api/src/lib/local-monitor-judge.ts /tmp/monitor-src/lib/local-monitor-judge.ts
COPY apps/api/src/services/monitoring/judgeChange.ts /tmp/monitor-src/services/monitoring/judgeChange.ts
COPY apps/api/src/lib/gcs-jobs.ts /tmp/monitor-src/lib/gcs-jobs.ts
COPY apps/api/src/lib/gcs-monitoring.ts /tmp/monitor-src/lib/gcs-monitoring.ts
COPY apps/api/src/services/logging/log_job.ts /tmp/monitor-src/services/logging/log_job.ts
COPY deployment/self-hosted-monitor/compile.cjs /tmp/compile-monitor.cjs
RUN node /tmp/compile-monitor.cjs
COPY deployment/self-hosted-monitor/storage.test.cjs /app/monitor-storage.test.cjs
RUN node --test /app/monitor-storage.test.cjs
CMD ["node", "dist/src/harness.js", "--start-docker"]
