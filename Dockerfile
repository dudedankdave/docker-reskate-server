# syntax=docker/dockerfile:1
#
# ReSkate dedicated server.
#
# Build context must contain a ./Server directory with the files from the game's
# dedicated-server download (not committed to git, they are proprietary):
#   Server/ReSkateServer  Server/libsteam_api.so  Server/libtier0_s.so
#   Server/libvstdlib_s.so  Server/steamclient.so
#
#   docker build -t dudedankdave/reskate-server:dev .

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
RUN apt-get update \
 && apt-get install -y --no-install-recommends libssl3t64 ca-certificates python3-minimal tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 reskate \
 && mkdir -p /data /home/reskate/.steam/sdk64 \
 && chown reskate:reskate /data /home/reskate/.steam /home/reskate/.steam/sdk64
WORKDIR /app
# --chown on COPY avoids a second 49 MB layer from a recursive chown
COPY --from=server --chown=reskate:reskate /out/ /app/
COPY --chown=reskate:reskate entrypoint.py /app/entrypoint.py
RUN ln -s /data/Mods /app/Mods \
 && ln -s /data/world-layers.json /app/world-layers.json \
 && ln -s /data/ReSkateServer.log /app/ReSkateServer.log \
 && ln -s /app/steamclient.so /home/reskate/.steam/sdk64/steamclient.so \
 && echo 3354750 > /app/steam_appid.txt \
 && chown -h reskate:reskate /app/Mods /app/world-layers.json /app/ReSkateServer.log /app/steam_appid.txt \
    /home/reskate/.steam/sdk64/steamclient.so
ENV LD_LIBRARY_PATH=/app SteamAppId=3354750 HOME=/home/reskate
USER reskate
VOLUME /data
EXPOSE 27015/udp 27016/udp
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/entrypoint.py"]
