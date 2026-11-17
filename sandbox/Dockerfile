# Linux environment for Specula: runs its test suite now, and serves as the
# agent sandbox later (agents see only what is mounted into the container).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# python3 (3.12) lands in /usr/bin, which Specula's tests hardcode;
# openjdk-21 puts java in /usr/bin too. procps provides ps; rsync copies the source in.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates coreutils curl git jq procps rsync \
        python3 python3-venv python3-pip \
        openjdk-21-jdk-headless maven \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Same uv version as on the Mac host.
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /usr/local/bin/

# Non-root user: tests that check permission handling behave differently as root.
RUN useradd --create-home --shell /bin/bash specula
USER specula
WORKDIR /home/specula

# Keep uv's virtualenv and cache inside the container, never in the mounted checkout.
ENV UV_PROJECT_ENVIRONMENT=/home/specula/venv \
    UV_CACHE_DIR=/home/specula/.cache/uv \
    UV_LINK_MODE=copy
