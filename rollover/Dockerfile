# The rollover service (see README.md). Standard library only.
FROM python:3.13-alpine
COPY rollover.py /app/rollover.py
# The Cocojambo yards; mount your own over /config/yards.json
COPY yards.example.json /config/yards.json
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-I", "/app/rollover.py"]
