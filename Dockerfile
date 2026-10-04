# NOTE: reconstructed from `docker history` of the published image. The first stage
# (which produces /out/ReSkateServer and /out/*.so) was not recoverable and must be
# supplied; everything below matches the published runtime stage.
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libssl3t64 ca-certificates python3-minimal tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 reskate
WORKDIR /app
COPY out/ReSkateServer out/*.so /app/
COPY entrypoint.py /app/entrypoint.py
RUN mkdir -p /data /home/reskate/.steam/sdk64 \
 && ln -s /data/Mods /app/Mods \
 && ln -s /data/world-layers.json /app/world-layers.json \
 && ln -s /data/ReSkateServer.log /app/ReSkateServer.log \
 && ln -s /app/steamclient.so /home/reskate/.steam/sdk64/steamclient.so \
 && echo 3354750 > /app/steam_appid.txt \
 && chown -R reskate:reskate /app /data /home/reskate
ENV LD_LIBRARY_PATH=/app SteamAppId=3354750 HOME=/home/reskate
USER reskate
VOLUME /data
EXPOSE 27015/udp 27016/udp
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/entrypoint.py"]
