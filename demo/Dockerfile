# syntax=docker/dockerfile:1
#
# vouch demo — one image, one command. Bundles the vouch server (built from
# THIS checkout, so it carries the newest kb.* surface incl. delete / archive /
# supersede), the vouch-ui web console, and seeds a starter knowledge base on
# first run. `docker compose up` in this folder, open the browser, and explore
# a populated, review-gated KB.
#
# Two runtimes in one image on purpose: python runs `vouch serve`, node serves
# the console via `vite preview` (which reuses the console's own /proxy/*
# middleware, pinned at the in-container vouch endpoint).

# ---- stage 1: build the console to static (keep the tree for vite preview) ---
FROM node:22-slim AS web
WORKDIR /web
COPY webapp/package.json webapp/package-lock.json ./
RUN npm ci
COPY webapp/ ./
RUN npm run build

# ---- stage 2: runtime = node (console) + python (vouch from source) ---------
FROM node:22-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    NODE_ENV=production \
    VOUCH_UI_ALLOW_REMOTE=1 \
    VOUCH_TARGET=http://127.0.0.1:8731 \
    VOUCH_HTTP_TOKEN=vouch-demo \
    VOUCH_DATA_DIR=/data \
    ANTHROPIC_MODEL=claude-sonnet-4-5

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv curl \
    && rm -rf /var/lib/apt/lists/*

# vouch from this checkout — the [web] extra brings the HTTP transport in.
# hatch_build.py is the build hook pyproject.toml points at; without it the
# sdist build backend fails before pip ever resolves dependencies.
COPY pyproject.toml README.md hatch_build.py /src/
COPY src /src/src
COPY adapters /src/adapters
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir "/src[web]"
ENV PATH="/opt/venv/bin:$PATH"

# the built console tree (vite preview needs vite + plugins + dist/ at runtime)
COPY --from=web /web /app/webapp

# bring-your-own-key LLM shim: reads a prompt on stdin, calls the Anthropic
# Messages API from ANTHROPIC_API_KEY. Wired in as compile.llm_cmd by the
# entrypoint only when a key is present. Stdlib only — runs on the venv python.
COPY demo/vouch-llm.py /usr/local/bin/vouch-llm
RUN chmod +x /usr/local/bin/vouch-llm


COPY demo/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data"]
EXPOSE 5173
ENTRYPOINT ["/entrypoint.sh"]
