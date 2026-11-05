# GPU batch lane images (homelab#1935).
#
#   runner   ubuntu:24.04 + the gpu_batch runner (stdlib Python) + psql.
#            The base for every GPU batch workload. Not published on its own
#            until a second workload needs it.
#   blender  runner + Blender, published as harbor.sirddail.net/ai/gpu-batch-blender.
#
# A new workload (ffmpeg, PyTorch, ...) is another `FROM runner AS <x>` stage
# plus a build step, not a new repo.

FROM ubuntu:26.04 AS runner

# psql: the guard's Obico tie-breaker (read-only role, column-level grant).
# ca-certificates: the in-cluster API read of that role's Secret.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 postgresql-client ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY src/gpu_batch /opt/gpu-batch/gpu_batch
ENV PYTHONPATH=/opt/gpu-batch \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# uid 1000 matches the laptop user that writes job dirs onto the NFS share.
USER 1000:1000
ENTRYPOINT ["python3", "-m", "gpu_batch.runner"]


FROM runner AS blender

USER root
# X/GL runtime libs Blender links against even in background mode. This is
# the set the 2026-11-05 probe on node-03 needed; nothing more.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 libice6 \
      libgl1 libegl1 libglu1-mesa libxext6 xz-utils curl \
 && rm -rf /var/lib/apt/lists/*

# Official tarball, verified against the sha256 blender.org publishes in
# blender-<ver>.sha256. Bump both together.
ARG BLENDER_VERSION=5.2.2
ARG BLENDER_SHA256=84098912789dc450e95697c4184fb8a90acbe5111c2ba4aede3fecb57806a168
RUN set -eux; \
    major_minor="${BLENDER_VERSION%.*}"; \
    curl -fsSL -o /tmp/blender.tar.xz \
      "https://download.blender.org/release/Blender${major_minor}/blender-${BLENDER_VERSION}-linux-x64.tar.xz"; \
    echo "${BLENDER_SHA256}  /tmp/blender.tar.xz" | sha256sum -c -; \
    mkdir -p /opt/blender; \
    tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1; \
    rm /tmp/blender.tar.xz; \
    ln -s /opt/blender/blender /usr/local/bin/blender; \
    apt-get purge -y curl xz-utils && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# compute,utility is all Cycles needs (OptiX/CUDA). No graphics capability,
# no runtimeClassName: verified on node-03.
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    BLENDER_VERSION=${BLENDER_VERSION}

USER 1000:1000
