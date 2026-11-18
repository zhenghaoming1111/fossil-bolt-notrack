# Build context: the pinned Ubuntu Base tarball and the staged Fossil project.
FROM scratch
ADD ubuntu-base.tar.gz /
ENV DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::ForceIPv4=true install -y --no-install-recommends \
       build-essential clang-18 python3 time util-linux binutils ca-certificates \
       zlib1g libssl3t64 libzstd1 libssl-dev zlib1g-dev tcl tcl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/project
COPY project/ ./
ENV TOOLS_ROOT=/opt/project/artifacts/mainline-options \
    PATH=/opt/project/artifacts/mainline-options/common/bin:${PATH} \
    FOSSIL_ORIGINAL=/opt/project/artifacts/source-fossil/bin/fossil.orig
LABEL org.opencontainers.image.title="Fossil with LLVM BOLT NOTRACK jump-table recovery" \
      org.opencontainers.image.revision="af3fdf39125a6f119bfcc97f1dd00572185f6e07"
CMD ["bash"]
