# syntax=docker/dockerfile:1.7
ARG BASE_API
FROM ${BASE_API}
ARG BASE_API
ARG BASE_IMAGE_ID
LABEL org.firecrawl.patch.base="${BASE_API}"
LABEL org.firecrawl.patch.base-image-id="${BASE_IMAGE_ID}"
RUN --mount=type=bind,source=apps/api/pnpm-lock.yaml,target=/tmp/patch-lock.yaml \
    --mount=type=bind,source=deployment/patched-base/patch.cjs,target=/tmp/patch.cjs \
    --mount=type=bind,source=deployment/patched-base/patches.json,target=/tmp/patches.json \
    node /tmp/patch.cjs
