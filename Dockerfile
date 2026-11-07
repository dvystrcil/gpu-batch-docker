# GPU batch lane images (homelab#1935).
#
#   runner   ubuntu:26.04 + the gpu_batch runner (stdlib Python) + psql.
#            The base for every GPU batch workload. Not published on its own
#            until a second workload needs it.
#   blender  runner + Blender, published as harbor.sirddail.net/ai/gpu-batch-blender.
#   freecad  runner + FreeCAD (freecadcmd, Fasteners, xvfb-run), published as
#            harbor.sirddail.net/ai/gpu-batch-freecad. CPU-only.
#   prusaslicer  runner + PrusaSlicer (Flathub build, run without flatpak),
#            published as harbor.sirddail.net/ai/gpu-batch-prusaslicer. CPU-only.
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


# PrusaSlicer for the vt350 slice checks (vt350#70's slice-check.py). Linux
# builds ship only through Flathub (the GitHub releases carry no Linux
# AppImage since 2.9), so this throwaway stage installs Flathub's build with
# flatpak and the final stage copies just its files. Nothing runs through
# flatpak/bwrap at runtime: bwrap needs user namespaces, which a pod's
# seccomp profile refuses.
#
# Both pins are ostree commits, i.e. content hashes of the whole tree, so
# they are the integrity check. They are the commits the laptop runs
# (`flatpak info com.prusa3d.PrusaSlicer` / `org.gnome.Platform//50`), so a
# cluster slice is byte-comparable with a laptop slice. Bump all four
# together; Renovate can't see Flathub.
FROM ubuntu:26.04 AS prusaslicer-fetch
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG PRUSASLICER_VERSION=2.9.7
ARG PRUSASLICER_COMMIT=22caa5995fef8170cbed1d404f993996e5552e47159391666a7b096a85d8f641
ARG GNOME_RUNTIME=50
ARG GNOME_RUNTIME_COMMIT=4337a389125f814785bd83f4ace01da5b6baa80fdcf5a9c81e60dcd03896c067
RUN apt-get update \
 && apt-get install -y --no-install-recommends flatpak ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    app=app/com.prusa3d.PrusaSlicer/x86_64/stable; \
    rt=runtime/org.gnome.Platform/x86_64/${GNOME_RUNTIME}; \
    flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo; \
    flatpak install --system -y --noninteractive --no-related flathub "$app"; \
    flatpak update --system -y --noninteractive --no-related --no-deps --commit="${PRUSASLICER_COMMIT}" "$app"; \
    flatpak update --system -y --noninteractive --no-related --no-deps --commit="${GNOME_RUNTIME_COMMIT}" "$rt"; \
    test "$(flatpak info --system --show-commit "$app")" = "${PRUSASLICER_COMMIT}"; \
    test "$(flatpak info --system --show-commit "$rt")" = "${GNOME_RUNTIME_COMMIT}"; \
    flatpak info --system "$app" | grep -qE "^ *Version: ${PRUSASLICER_VERSION}$"; \
    flatpak info --system "$app" | grep -qE "^ *Runtime: org.gnome.Platform/x86_64/${GNOME_RUNTIME}$"; \
    mkdir -p /out/runtime; \
    cp -a "$(flatpak info --system --show-location "$app")/files" /out/app; \
    cp -a "$(flatpak info --system --show-location "$rt")/files/lib" /out/runtime/lib


FROM runner AS prusaslicer

# CPU-only: runs on max-01 with GPU_BATCH_PRINT_GUARD=off (no GPU visible).
# The binary runs on the runtime's own loader and libs, never Ubuntu's.
# rsvg-convert: slice-check.py turns its G-code plot (SVG) into the PNG preview.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends librsvg2-bin \
 && rm -rf /var/lib/apt/lists/*
COPY --from=prusaslicer-fetch /out/app /opt/prusaslicer/app
COPY --from=prusaslicer-fetch /out/runtime /opt/prusaslicer/runtime
RUN set -eux; \
    rtlib=/opt/prusaslicer/runtime/lib/x86_64-linux-gnu; \
    printf '#!/bin/sh\nexec %s/ld-linux-x86-64.so.2 --library-path /opt/prusaslicer/app/lib:%s /opt/prusaslicer/app/bin/prusa-slicer "$@"\n' "$rtlib" "$rtlib" \
      > /usr/local/bin/prusa-slicer; \
    chmod 755 /usr/local/bin/prusa-slicer

ARG PRUSASLICER_VERSION=2.9.7
ENV PRUSASLICER_VERSION=${PRUSASLICER_VERSION}

USER 1000:1000
