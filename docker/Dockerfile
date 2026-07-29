# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# meta-optimizer container image.
#
# Two targets:
#   runtime      production image; project installed non-editable, no dev tools
#   development  extends runtime with the `dev` extra, tests, and an editable
#                install so a bind-mounted source tree is what actually runs
#
# Build context is the repository root, not this directory:
#   docker build -f docker/Dockerfile --target development .
#
# No system packages are installed: psycopg[binary] vendors its own libpq, so
# there is nothing to compile and no libpq-dev to keep patched.
# ---------------------------------------------------------------------------

FROM python:3.13-slim AS base

# PYTHONUNBUFFERED       stdout/stderr are not block-buffered, so Rich output
#                        and log records appear immediately rather than being
#                        held until the process exits.
# PYTHONDONTWRITEBYTECODE  no .pyc files written into the bind-mounted source
#                        tree, where they would be owned by the container user.
# PIP_NO_CACHE_DIR       the wheel cache is dead weight in an image layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# The container runs as an unprivileged user. The UID and GID are build
# arguments so they can be matched to the host user on Linux, where a mismatch
# would make files created in the bind mount unwritable from the host. On
# macOS and Windows the Docker Desktop file sharing layer remaps ownership
# transparently and the default of 1000 is always correct.
ARG APP_UID=1000
ARG APP_GID=1000

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /bin/bash app

WORKDIR /workspace

RUN python -m pip install --upgrade pip


# ---------------------------------------------------------------------------
# runtime — production image
# ---------------------------------------------------------------------------

FROM base AS runtime

# pyproject.toml and the package are copied together because the build backend
# needs both to produce wheel metadata; copying the manifest alone would fail.
# Paths are copied explicitly rather than with `COPY . .` so that .env, .git,
# and any other local artefact can never be baked into a distributable image.
# README.md is copied because pyproject.toml declares it as the package readme;
# the build backend reads it to produce wheel metadata and fails without it.
COPY pyproject.toml alembic.ini README.md ./
COPY app ./app
COPY migrations ./migrations

RUN pip install .

RUN chown -R app:app /workspace

USER app

CMD ["meta", "--help"]


# ---------------------------------------------------------------------------
# development — image used by docker compose
# ---------------------------------------------------------------------------

FROM runtime AS development

USER root

COPY tests ./tests

# Editable install: import of `app` resolves to /workspace/app at runtime, so
# the bind-mounted host source is executed directly and a code change needs no
# rebuild. The `dev` extra adds pytest, ruff, black, and mypy, which the
# runtime image deliberately does not carry.
RUN pip install -e ".[dev]"

RUN chown -R app:app /workspace

USER app

# Idle by default so that `docker compose up -d` leaves a long-lived container
# to exec into. Ad-hoc invocations do not need it:
#   docker compose run --rm app meta campaigns
CMD ["tail", "-f", "/dev/null"]
