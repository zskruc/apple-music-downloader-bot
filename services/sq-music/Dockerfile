# Audited upstream 3.1.33. Review licensing before obtaining/building upstream.
ARG SQ_BASE_IMAGE=ghcr.io/59799517/simple_sq_music_plus@sha256:4f3e499c07da526bea5648fc4e1b9c449e363356c5e8c1ef4d96c2d67b1e20f2
FROM ${SQ_BASE_IMAGE} AS patch-build
COPY patched/ /tmp/sq-patched/
RUN test -f /app/BOOT-INF/classes/application.yml && \
    grep -q '^version: 3.1.33$' /app/BOOT-INF/classes/application.yml && \
    javac -J-Xms64m -J-Xmx512m -proc:none -parameters --release 21 \
      -cp '/app/BOOT-INF/classes:/app/BOOT-INF/lib/*' \
      -d /tmp/sq-compiled \
      /tmp/sq-patched/com/sqmusicplus/v3/plug/base/hander/SearchHanderAbstract.java \
      /tmp/sq-patched/com/sqmusicplus/v3/controller/TaskController.java

FROM ${SQ_BASE_IMAGE}
COPY --from=patch-build /tmp/sq-compiled/ /app/BOOT-INF/classes/
LABEL org.opencontainers.image.source="https://github.com/59799517/simple_sq_music_plus" \
      org.opencontainers.image.revision="cf0f6ed136be0aa13845bbab028c27e404b89d5d" \
      local.sqmusic.bot-bridge="1"
