# Образ советника стюарда (#1654): OpenCode — инструмент из списка подписки
# GLM Coding Plan (docs.z.ai/devpack/usage-policy). Самостоятельный рецепт:
# от неописанных образов ревью не зависит.
#
# Сборка (под пользователем ревьюера, из корня репозитория):
#   podman build -t localhost/haiplane-advisor:1 -f deploy/review-runner/advisor.Containerfile deploy/review-runner
#   printf 'localhost/haiplane-advisor:1\n' > /etc/haiplane-review/advisor-image
FROM docker.io/library/debian:12-slim

ARG NODE_VERSION=22.11.0
ARG OPENCODE_VERSION=1.18.35

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates jq xz-utils \
 && rm -rf /var/lib/apt/lists/*

# Node 22 из официального архива (версия закреплена, не NodeSource-скрипт).
RUN case "$(dpkg --print-architecture)" in \
      amd64) NA=x64 ;; \
      arm64) NA=arm64 ;; \
      *) echo "архитектура не поддержана" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NA}.tar.xz" \
    | tar -xJ -C /usr/local --strip-components=1 \
 && node --version

RUN npm install -g opencode-ai@1.18.35 && opencode --version

# Чистая конфигурация: без шаринга, автообновления, плагинов, MCP, LSP,
# форматтеров и снимков файлов. Путь закреплён переменной OPENCODE_CONFIG
# (packages/core/src/flag/flag.ts:21 тега v1.18.35).
RUN mkdir -p /etc/opencode /work
COPY advisor-opencode.json /etc/opencode/opencode.json
ENV OPENCODE_CONFIG=/etc/opencode/opencode.json

ENTRYPOINT []
WORKDIR /work
