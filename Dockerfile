# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG DENO_IMAGE=denoland/deno:bin-2.9.6@sha256:4cf0029b9aeeeed5efcbb71828737f0d7c8c8a20072df960e51a5679ef0d21ba
FROM ${DENO_IMAGE} AS deno

FROM debian:bookworm-slim@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8 AS whisper-builder
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
ARG INSTALL_YT_SABR=false
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
COPY services/youtube/install_sabr_tool.py /usr/local/libexec/install_sabr_tool.py
COPY licenses/yt-dlp-sabr-Unlicense.txt /usr/share/doc/yt-dlp-sabr/LICENSE
COPY licenses/yt-dlp-sabr-THIRD_PARTY_LICENSES.txt /usr/share/doc/yt-dlp-sabr/THIRD_PARTY_LICENSES.txt
COPY services/youtube/SABR_SOURCES.md /usr/share/doc/yt-dlp-sabr/SOURCES.md
RUN case "${INSTALL_YT_SABR}" in \
      false) ;; \
      true) python /usr/local/libexec/install_sabr_tool.py ;; \
      *) printf '%s\n' 'INSTALL_YT_SABR must be true or false' >&2; exit 1 ;; \
    esac
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m pip check
COPY bot/ /app/
COPY ops/lyrics_network_policy.py /app/lyrics_network_policy.py
RUN python -m compileall -q /app \
    && python -c 'import ingest_bot, wrapper_v2_adapter, lyrics_network_policy, lyrics_network_policy_runner, word_lyrics_automation.source_worker, word_lyrics_automation.nas_alignment_worker, word_lyrics_automation.service_health, word_lyrics_automation.media_integrity, word_lyrics_automation.player_verification' \
    && python -m gamdl --version && python -m yt_dlp --version && deno --version
LABEL org.opencontainers.image.title="Apple Music Downloader Bot" \
      org.opencontainers.image.version="${RELEASE_VERSION}"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/ingest_bot.py"]
