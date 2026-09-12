# Same official image, pulled from AWS ECR Public's mirror of Docker Official Images
# instead of Docker Hub. A Railway build failed with an i/o timeout resolving
# registry-1.docker.io before touching any of this repo's code, and Docker Hub also
# rate-limits anonymous pulls; the mirror avoids both.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache bust: update this when you need to force a fresh build
ARG CACHEBUST=1
COPY . .

# Chat state (topics, seen-paper history) must live on a mounted Railway volume --
# the container filesystem is wiped on every redeploy. chats_store reads DATA_DIR.
ENV DATA_DIR=/data

EXPOSE 8080

CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}
