# The rollover service (see README.md). Standard library only.
FROM python:3.13-alpine
COPY rollover.py /app/rollover.py
# The Cocojambo servers; mount your own over /config/servers.json
COPY servers.example.json /config/servers.json
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-I", "/app/rollover.py"]
