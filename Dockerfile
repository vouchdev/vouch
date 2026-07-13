# The canonical vouch container, published to ghcr.io/vouchdev/vouch on every
# release tag by .github/workflows/release.yml. General-purpose: the
# entrypoint is the `vouch` CLI itself and the default command is the stdio
# MCP server, so the same image serves MCP hosts, the HTTP transport, and
# one-off CLI calls.
#
#   MCP (stdio, the canonical surface — note -i):
#     docker run -i --rm -v "$PWD:/data" ghcr.io/vouchdev/vouch
#   HTTP transport (refuses a public bind without a bearer token):
#     docker run --rm -p 8731:8731 -v "$PWD:/data" -e VOUCH_HTTP_TOKEN=... \
#       ghcr.io/vouchdev/vouch serve --transport http --host 0.0.0.0 --allow-public
#   CLI:
#     docker run --rm -v "$PWD:/data" ghcr.io/vouchdev/vouch status
#
# Bind-mount the project root (the directory containing .vouch/) at /data;
# the KB is discovered from the working directory exactly as on a host
# checkout. adapters/http-tunnel/Dockerfile remains the opinionated
# HTTP-only deployment reference.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

LABEL org.opencontainers.image.source="https://github.com/vouchdev/vouch" \
      org.opencontainers.image.description="Git-native, review-gated knowledge base for LLM agents. MCP server + CLI." \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install from this checkout so local dev images never need a PyPI release
# in the loop. The web extra pulls in the review-ui / fastapi surface;
# embeddings stay out (torch does not belong in the default image).
# adapters/ is force-included into the wheel (vouch install-mcp resolves
# the templates from the installed package), so the context must carry it.
COPY pyproject.toml README.md ./
COPY src ./src
COPY adapters ./adapters
RUN pip install --no-cache-dir '.[web]'

# /data is the KB volume mount point: the host's project root, containing
# .vouch/, is served exactly as-is.
VOLUME ["/data"]
WORKDIR /data

# uid 1000 matches the default first user on Linux hosts, so files created
# through the /data bind mount stay owned by the host user. Override with
# `docker run --user "$(id -u):$(id -g)"` where that assumption is wrong.
RUN useradd --create-home --uid 1000 vouch && chown vouch:vouch /data
USER vouch

EXPOSE 8731

ENTRYPOINT ["vouch"]
CMD ["serve"]
