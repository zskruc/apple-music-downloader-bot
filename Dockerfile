# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG DENO_VERSION=2.9.6
FROM denoland/deno:bin-${DENO_VERSION} AS deno

FROM debian:bookworm-slim AS whisper-builder
ARG WHISPER_CPP_REVISION=4979e04f5dcaccb36057e059bbaed8a2f5288315
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init && git remote add origin https://github.com/ggml-org/whisper.cpp.git \
    && git fetch --depth 1 origin "${WHISPER_CPP_REVISION}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${WHISPER_CPP_REVISION}" \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
       -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
       -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF \
    && cmake --build build --target whisper-cli -j2

FROM ${PYTHON_IMAGE}
ARG RELEASE_VERSION=source
ARG WHISPER_CPP_REVISION=4979e04f5dcaccb36057e059bbaed8a2f5288315
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl ffmpeg tini libstdc++6 fontconfig fonts-noto-cjk fonts-liberation2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
COPY licenses/Deno-MIT.txt /usr/share/doc/deno/LICENSE
COPY --from=whisper-builder /src/build/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=whisper-builder /src/LICENSE /usr/share/doc/whisper.cpp/LICENSE
RUN printf '%s\n' "https://github.com/ggml-org/whisper.cpp/tree/${WHISPER_CPP_REVISION}" \
    > /usr/share/doc/whisper.cpp/SOURCE
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m pip check
COPY bot/ /app/
RUN python -m compileall -q /app \
    && python -c 'import ingest_bot, wrapper_v2_adapter, word_lyrics_automation.source_worker, word_lyrics_automation.nas_alignment_worker' \
    && python -m gamdl --version && python -m yt_dlp --version && deno --version
LABEL org.opencontainers.image.title="Apple Music Downloader Bot" \
      org.opencontainers.image.version="${RELEASE_VERSION}"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/ingest_bot.py"]
