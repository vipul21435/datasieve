# check=skip=FromPlatformFlagConstDisallowed
# DataSieve runtime image: the curator CLI with the bundled demo, no dev tools.
# The optional Rust suffix-array backend (third_party/) is deliberately not built.
#
#   docker build -t datasieve:dev .
#   docker run --rm datasieve:dev                       # runs the demo pipeline
#   docker run --rm datasieve:dev --help
#   docker run --rm -v "$PWD/data:/data" datasieve:dev run /data/my-spec.yaml
#
# python:3.12-slim, pinned by digest (docker pull python:3.12-slim; docker image
# inspect --format '{{index .RepoDigests 0}}' python:3.12-slim).
#
# linux/amd64 only: the upstream dependency polars-grouper publishes manylinux
# wheels for x86_64 but not aarch64, and building it needs a Rust toolchain and
# more memory than a small VM has. On Apple Silicon Docker runs the image under
# Rosetta/QEMU; that is fine for the demo and for small datasets. The constant
# platform is deliberate (it keeps 'docker build .' working on arm64 hosts), so
# the BuildKit lint rule for it is skipped on line 1 instead of warning on every build.
FROM --platform=linux/amd64 python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Same uv version that wrote uv.lock (see .github/actions/setup-python-env).
RUN pip install uv==0.11.29 \
    && useradd --create-home --uid 1000 --shell /usr/sbin/nologin curator

WORKDIR /app

# Runtime dependencies first, so source edits do not re-resolve the lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The project itself: both packages, the demo spec and its data, the docs the wheel embeds.
COPY LICENSE README.md ./
COPY src ./src
COPY examples ./examples
RUN uv sync --frozen --no-dev --no-editable \
    && chown -R curator:curator /app/examples

# CURATOR_WORK_DIR has to exist, owned by curator, before the USER switch: Docker
# initialises a named volume mounted there (docker-compose.yml, -v runs:/home/curator/runs)
# from the image's directory and its owner, and would otherwise create it root-owned.
RUN install -d -o curator -g curator /home/curator/runs

USER curator
ENV PATH="/opt/venv/bin:$PATH" \
    CURATOR_WORK_DIR=/home/curator/runs \
    CURATOR_LOG_FORMAT=text \
    CURATOR_LOG_LEVEL=WARNING

ENTRYPOINT ["python", "-m", "curator"]
CMD ["run", "examples/sft-demo.yaml"]
