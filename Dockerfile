# syntax=docker/dockerfile:1
#
# ReSkate dedicated server.
#
# Build context must contain a ./Server directory with the files from the game's
# dedicated-server download (not committed to git, they are proprietary):
#   Server/ReSkateServer  Server/libsteam_api.so  Server/libtier0_s.so
#   Server/libvstdlib_s.so  Server/steamclient.so
#
#   docker build --build-arg VERSION=1.1.0 -t dudedankdave/reskate-server:1.1.0 .

# ---- build stage: validate and stage the server files ----------------------
FROM debian:trixie-slim AS server
COPY Server/ /out/
RUN set -e; cd /out; \
    for f in ReSkateServer libsteam_api.so libtier0_s.so libvstdlib_s.so steamclient.so; do \
      [ -f "$f" ] || { echo "missing Server/$f in build context" >&2; exit 1; }; \
    done; \
    chmod 755 ReSkateServer; chmod 644 *.so

# ---- runtime stage ---------------------------------------------------------
FROM debian:trixie-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="reskate-server" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.source="https://github.com/dudedankdave/docker-reskate-server"
RUN apt-get update \
 && apt-get install -y --no-install-recommends libssl3t64 ca-certificates curl unzip python3-minimal tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 reskate \
 && mkdir -p /data /home/reskate/.steam/sdk64 \
 && chown reskate:reskate /data /home/reskate/.steam /home/reskate/.steam/sdk64
WORKDIR /app
# --chown on COPY avoids a second 49 MB layer from a recursive chown
COPY --from=server --chown=reskate:reskate /out/ /app/
COPY --chown=reskate:reskate entrypoint.py healthcheck.py notifier.py mods.py updater.py supervisor.py leaderboard.py /app/
RUN ln -s /data/Mods /app/Mods \
 && ln -s /data/world-layers.json /app/world-layers.json \
 && ln -s /data/ReSkateServer.log /app/ReSkateServer.log \
 && ln -s /app/steamclient.so /home/reskate/.steam/sdk64/steamclient.so \
 && echo 3354750 > /app/steam_appid.txt \
 && chown -h reskate:reskate /app/Mods /app/world-layers.json /app/ReSkateServer.log /app/steam_appid.txt \
    /home/reskate/.steam/sdk64/steamclient.so
ENV LD_LIBRARY_PATH=/app SteamAppId=3354750 HOME=/home/reskate RESKATE_IMAGE_VERSION=$VERSION
USER reskate
VOLUME /data
# Healthy once the log shows the server is up (see healthcheck.py). Startup needs a
# Steam sign-in, so give it time before failures count.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python3", "/app/healthcheck.py"]
EXPOSE 27015/udp 27016/udp
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/entrypoint.py"]
