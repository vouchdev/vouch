# Reference container for `vouch serve --transport http`.
#
# Build context expected to be the repo root, OR a directory containing the
# vouch-kb package on PyPI (uncomment the alternate install line below).
# Either way, the resulting image listens on :8731 and reads the KB at
# /data/.vouch — mount the host's .vouch/ into that path.

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VOUCH_HTTP_PORT=8731 \
    VOUCH_KB_PATH=/data

WORKDIR /app

# Option A (default): install from this checkout. Allows local dev images
# without a PyPI release in the loop.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Option B (uncomment if you'd rather pin a released version):
# RUN pip install --no-cache-dir vouch-kb>=0.2

# /data is the KB volume mount point. Hosts that bind-mount a directory
# containing .vouch/ here will be served as-is.
VOLUME ["/data"]
WORKDIR /data

# Drop privileges -- the server reads/writes only inside /data/.vouch and
# never spawns child processes, so a non-root UID is safe and prevents a
# bug from rooting the container.
RUN useradd --system --uid 10001 vouch && chown -R vouch:vouch /data
USER vouch

EXPOSE 8731

# Bind 0.0.0.0 + --allow-public is what makes the container externally
# reachable, but vouch refuses to start that combination unless at least one
# bearer token is configured (VOUCH_HTTP_TOKEN env, --token flag, or
# config.yaml serve.bearer_tokens). The deployment templates set
# VOUCH_HTTP_TOKEN via secrets — see README.md.
ENTRYPOINT ["vouch", "serve", "--transport", "http", \
            "--host", "0.0.0.0", "--port", "8731", \
            "--allow-public"]
