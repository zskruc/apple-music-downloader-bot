# wrapper-manager remains a separate AGPL-3.0 upstream component.
FROM golang:1.23-bookworm AS manager-builder
ARG WRAPPER_MANAGER_REVISION=d40d2046fead280f568c61a661cfdc10bd71c7dc
WORKDIR /src
RUN git init && git remote add origin https://github.com/WorldObservationLog/wrapper-manager.git \
    && git fetch --depth 1 origin "${WRAPPER_MANAGER_REVISION}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${WRAPPER_MANAGER_REVISION}" \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o /out/wrapper-manager \
    && cp LICENSE /out/LICENSE \
    && printf '%s\n' "https://github.com/WorldObservationLog/wrapper-manager/tree/${WRAPPER_MANAGER_REVISION}" > /out/SOURCE

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl genisoimage netcat-openbsd openssh-client \
    qemu-system-x86 qemu-utils procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=manager-builder /out/wrapper-manager /opt/payload/wrapper-manager
COPY --from=manager-builder /out/LICENSE /usr/share/doc/wrapper-manager/LICENSE
COPY --from=manager-builder /out/SOURCE /usr/share/doc/wrapper-manager/SOURCE
COPY vm/account-bridge.py vm/start-manager.sh vm/start-single.sh vm/provision.sh /opt/payload/
COPY vm/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh /opt/payload/*
ENTRYPOINT ["/entrypoint.sh"]
