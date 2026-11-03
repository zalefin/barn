# A clean machine for scripts/integration.sh: barn's prerequisites (README) and
# nothing else, with an unprivileged user. barn itself is installed at run time
# from the working tree.
FROM debian:13-slim

ARG TERRAFORM_VERSION=1.16.3
ARG AWSCLI_VERSION=2.23.6
ARG SSM_PLUGIN_VERSION=1.2.896.0
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) aws=x86_64; ssm=ubuntu_64bit ;; \
      arm64) aws=aarch64; ssm=ubuntu_arm64 ;; \
      *) echo "unsupported arch ${TARGETARCH}"; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo terraform.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH:-amd64}.zip"; \
    unzip -q terraform.zip terraform -d /usr/local/bin; \
    curl -fsSLo awscli.zip "https://awscli.amazonaws.com/awscli-exe-linux-${aws}-${AWSCLI_VERSION}.zip"; \
    unzip -q awscli.zip; ./aws/install; \
    curl -fsSLo ssm.deb "https://s3.amazonaws.com/session-manager-downloads/plugin/${SSM_PLUGIN_VERSION}/${ssm}/session-manager-plugin.deb"; \
    dpkg -i ssm.deb; \
    rm -rf /tmp/*

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /usr/local/bin/

RUN useradd --create-home --shell /bin/bash barn \
    && mkdir -p /home/barn/.cache/terraform-plugins && chown -R barn:barn /home/barn/.cache
USER barn
WORKDIR /home/barn
# The cache volume keeps uv's Python and Terraform's providers between runs.
ENV PATH=/home/barn/.local/bin:$PATH \
    TF_PLUGIN_CACHE_DIR=/home/barn/.cache/terraform-plugins
