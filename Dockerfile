# GPU batch lane images (homelab#1935).
#
#   runner   ubuntu:26.04 + the gpu_batch runner (stdlib Python) + psql.
#            The base for every GPU batch workload. Not published on its own
#            until a second workload needs it.
#   blender  runner + Blender, published as harbor.sirddail.net/ai/gpu-batch-blender.
#   freecad  runner + FreeCAD (freecadcmd, Fasteners, xvfb-run), published as
#            harbor.sirddail.net/ai/gpu-batch-freecad. CPU-only.
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


FROM runner AS freecad

# FreeCAD headless (freecadcmd) for the vt350 CAD checks (clashlib, the
# toolhead sweep, derive_stls) with the Fasteners workbench, plus xvfb-run
# for the one GUI-mode export (render_full.sh's export_meshes.py).
# CPU-only: runs on max-01, so no NVIDIA env.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb xauth libgl1 libegl1 libglu1-mesa libx11-6 libxext6 libxrender1 libxi6 \
      libxkbcommon0 libxkbcommon-x11-0 libfontconfig1 libdbus-1-3 curl git \
 && rm -rf /var/lib/apt/lists/*

# Official AppImage from the FreeCAD GitHub release, verified against the
# sha256 digest GitHub publishes for that release asset (FreeCAD publishes no
# separate checksum file). Extracted at build time: a pod has no FUSE. The
# extracted tree comes out 0700 (root only); go+rX lets uid 1000 run it.
# 26.3 final is not out yet; rc2 is the build the laptop runs. Bump both together.
ARG FREECAD_VERSION=26.3rc2
ARG FREECAD_SHA256=2cf4212d5a348574e786da7d5a8faeeb3e83dd4245c23898dede92ce82ce4908
# --appimage-extract writes squashfs-root/ into the working directory.
WORKDIR /tmp
RUN set -eux; \
    curl -fsSL -o /tmp/FreeCAD.AppImage \
      "https://github.com/FreeCAD/FreeCAD/releases/download/${FREECAD_VERSION}/FreeCAD_${FREECAD_VERSION}-Linux-x86_64.AppImage"; \
    echo "${FREECAD_SHA256}  /tmp/FreeCAD.AppImage" | sha256sum -c -; \
    chmod +x /tmp/FreeCAD.AppImage; \
    /tmp/FreeCAD.AppImage --appimage-extract >/dev/null; \
    mv /tmp/squashfs-root /opt/freecad; \
    chmod -R go+rX /opt/freecad; \
    rm /tmp/FreeCAD.AppImage; \
    printf '#!/bin/sh\nexec /opt/freecad/AppRun freecadcmd "$@"\n' > /usr/local/bin/freecadcmd; \
    printf '#!/bin/sh\nexec /opt/freecad/AppRun freecad "$@"\n' > /usr/local/bin/freecad; \
    chmod 755 /usr/local/bin/freecadcmd /usr/local/bin/freecad
WORKDIR /

# Fasteners workbench, pinned by commit (git checks the hash); the same
# version the laptop runs, so a model's FSScrew objects rebuild identically.
ARG FASTENERS_REF=V0.5.64-beta
ARG FASTENERS_COMMIT=85a21e2243433e38e8e59e1e29c3d607ea4e3729
RUN set -eux; \
    d=/opt/freecad/usr/Mod/fasteners; \
    git init -q "$d"; \
    git -C "$d" fetch -q --depth 1 https://github.com/shaise/FreeCAD_FastenersWB.git "${FASTENERS_COMMIT}"; \
    git -C "$d" checkout -q FETCH_HEAD; \
    test "$(git -C "$d" rev-parse HEAD)" = "${FASTENERS_COMMIT}"; \
    rm -rf "$d/.git"; \
    apt-get purge -y curl git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# FREECAD_APPIMAGE / FREECADCMD are what vt350's clashlib and sweep read, so
# they run unchanged.
ENV FREECAD_APPIMAGE=/opt/freecad/AppRun \
    FREECADCMD=/usr/local/bin/freecadcmd \
    FREECAD_VERSION=${FREECAD_VERSION} \
    FASTENERS_REF=${FASTENERS_REF}

USER 1000:1000
