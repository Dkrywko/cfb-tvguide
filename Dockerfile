# Chainguard publishes its free images as :latest only, so the digests are the pins.
FROM cgr.dev/chainguard/python:latest-dev@sha256:c179c7d8e23caed59a51ff74d4742f8f8d3f16af4c33c40acaf8d3ee07234592 AS prep
USER root
RUN apk add --no-cache tzdata && mkdir /cache

FROM cgr.dev/chainguard/python:latest@sha256:fd6d0771e8ec99b1710dfc3d72ca19ba2ff6b14f72b7978922ab0c8acb72c3f3
# The runtime image ships no zone files, and kickoff times need real DST rules.
COPY --from=prep /usr/share/zoneinfo /usr/share/zoneinfo
# Owned by the runtime user, so a fresh volume mounted here starts writable.
COPY --from=prep --chown=65532:65532 /cache /cache
COPY cfb_tvguide.py /app/cfb_tvguide.py
ENV XDG_CACHE_HOME=/cache
EXPOSE 8080
ENTRYPOINT ["python", "/app/cfb_tvguide.py"]
CMD ["--serve", "--host", "0.0.0.0", "--port", "8080"]
