# syntax=docker/dockerfile:1
#
# Disposable full-system E2E image for Dridh Phase 3.
#
# Pinned: Python 3.13.18 (base image), Node.js 24.22.2 with its bundled npm 11.19.1
# (verified against the official SHASUMS256 list), uv 0.13.7, OpenCode CLI 2.2.6
# (lock-pinned in .github/scripts/opencode-cli). The image contains no credentials, no
# host configuration and no Docker socket. Build context: the repository root.

FROM python:3.13.18-slim-bookworm AS runtime-base

ARG NODE_VERSION=24.22.2
ARG NPM_VERSION=11.19.1
ARG UV_VERSION=0.13.7

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils procps git \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) NODE_ARCH=x64 ;; \
      arm64) NODE_ARCH=arm64 ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    base="https://nodejs.org/dist/v${NODE_VERSION}"; \
    file="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"; \
    cd /tmp; \
    curl -fsSLO "${base}/${file}"; \
    curl -fsSL -o SHASUMS256.txt "${base}/SHASUMS256.txt"; \
    grep " ${file}\$" SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "${file}" -C /usr/local --strip-components=1; \
    rm -f "${file}" SHASUMS256.txt; \
    test "$(node --version)" = "v${NODE_VERSION}"; \
    test "$(npm --version)" = "${NPM_VERSION}"

RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
 && test "$(uv --version | cut -d' ' -f2)" = "${UV_VERSION}"

# ---- build stage: plugin, packaged assets and the Dridh wheel, from this source tree
FROM runtime-base AS build

WORKDIR /src
COPY dridh /src/dridh
RUN cd /src/dridh/opencode/plugins/dridh-bridge \
 && npm ci --no-audit --no-fund \
 && npm run build
RUN cd /src/dridh \
 && python3 scripts/build_assets.py \
 && uv build --wheel --out-dir /dist

# ---- OpenCode CLI, pinned by the lock file used by Phase 1.5 CI
FROM runtime-base AS opencode

COPY .github/scripts/opencode-cli/package.json .github/scripts/opencode-cli/package-lock.json /opt/opencode-cli/
RUN cd /opt/opencode-cli \
 && npm ci --no-audit --no-fund \
 && test "$(./node_modules/.bin/opencode --version)" = "opencode v2.2.6"

# ---- runtime image for the scenario
FROM runtime-base AS e2e

RUN groupadd --gid 10001 e2e \
 && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash e2e \
 && python3 -m venv /opt/dridh/venv
COPY --from=build /dist/ /opt/dridh/wheels/
RUN /opt/dridh/venv/bin/pip install --no-cache-dir /opt/dridh/wheels/*.whl \
 && /opt/dridh/venv/bin/dridh --version
COPY --from=opencode /opt/opencode-cli /opt/opencode-cli
COPY dridh/tests/e2e /opt/e2e

ENV PATH=/opt/dridh/venv/bin:/usr/local/bin:/usr/bin:/bin \
    DRIDH_BIN=/opt/dridh/venv/bin/dridh \
    DRIDH_OPENCODE_BIN=/opt/opencode-cli/node_modules/.bin/opencode \
    E2E_EVIDENCE_DIR=/work/evidence

RUN mkdir -p /work/evidence && chown -R e2e:e2e /work
USER e2e
WORKDIR /work
CMD ["python3", "/opt/e2e/scenario.py"]
