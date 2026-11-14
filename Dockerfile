FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PMKT_DATA_DIR=/data
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir .
VOLUME /data
ENTRYPOINT ["pmkt"]
