# syntax=docker/dockerfile:1
#
# Slim (distroless) build of the ReSkate dedicated server: no shell, no package manager, no Python,
# no curl. Build context must be the directory holding ./Server (the proprietary files) and
# ./slim, e.g.:
#   docker build -f slim/Dockerfile --build-arg VERSION=1.1.7 -t dudedankdave/reskate-server:1.1.7 .

# ---- the one static Go binary (entrypoint, Discord sidecar, healthcheck, curl stand-in) ----
FROM golang:1.24-alpine AS gobuild
WORKDIR /src
COPY slim/go.mod slim/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/reskate .

# ---- assemble the filesystem: server files, tini, a static busybox for `sh` ----------------
FROM debian:trixie-slim AS rootfs
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini busybox-static \
 && rm -rf /var/lib/apt/lists/*
COPY Server/ /rootfs/app/
RUN set -e; cd /rootfs/app; \
    for f in ReSkateServer libsteam_api.so libtier0_s.so libvstdlib_s.so steamclient.so; do \
      [ -f "$f" ] || { echo "missing Server/$f in build context" >&2; exit 1; }; \
    done; \
    chmod 755 ReSkateServer; chmod 644 *.so
COPY --from=gobuild /out/reskate /rootfs/app/reskate
# The server runs `sh -c "curl ..."` for the global ban list: sh = static busybox, curl = our binary.
RUN set -e; mkdir -p /rootfs/usr/bin /rootfs/bin /rootfs/usr/local/bin /rootfs/data /rootfs/home/reskate/.steam/sdk64; \
    cp /usr/bin/tini /rootfs/usr/bin/tini; \
    cp /bin/busybox /rootfs/bin/busybox; ln -s busybox /rootfs/bin/sh; \
    ln -s /app/reskate /rootfs/usr/local/bin/curl; \
    ln -s /data/Mods /rootfs/app/Mods; \
    ln -s /data/world-layers.json /rootfs/app/world-layers.json; \
    ln -s /data/ReSkateServer.log /rootfs/app/ReSkateServer.log; \
    ln -s /app/steamclient.so /rootfs/home/reskate/.steam/sdk64/steamclient.so; \
    echo 3354750 > /rootfs/app/steam_appid.txt

# ---- runtime ---------------------------------------------------------------------------
FROM gcr.io/distroless/cc-debian13
ARG VERSION=dev
LABEL org.opencontainers.image.title="reskate-server" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.source="https://github.com/dudedankdave/docker-reskate-server"
COPY --from=rootfs /rootfs/usr /usr
COPY --from=rootfs /rootfs/bin /bin
COPY --from=rootfs --chown=1000:1000 /rootfs/app /app
COPY --from=rootfs --chown=1000:1000 /rootfs/data /data
COPY --from=rootfs --chown=1000:1000 /rootfs/home /home
ENV LD_LIBRARY_PATH=/app SteamAppId=3354750 HOME=/home/reskate RESKATE_IMAGE_VERSION=$VERSION \
    PATH=/usr/local/bin:/usr/bin:/bin
WORKDIR /app
USER 1000:1000
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 CMD ["/app/reskate", "healthcheck"]
EXPOSE 27015/udp 27016/udp
ENTRYPOINT ["/usr/bin/tini", "--", "/app/reskate"]
