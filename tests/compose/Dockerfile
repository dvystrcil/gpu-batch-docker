# Harness image (gpu-batch-docker#10): the runner stage (it already has
# python3) plus the mocks, fixtures, seed and assert scripts. No extra base
# image to pull or pin. /work is created uid 1000 so the fresh named volume
# mounted there inherits that owner.
ARG RUNNER_IMAGE=gpu-batch-runner:local
# hadolint ignore=DL3006
FROM ${RUNNER_IMAGE}
USER root
RUN mkdir -p /work && chown 1000:1000 /work
COPY mocks/moonraker_mock.py mocks/notify_sink.py /harness/mocks/
COPY fixtures/ /harness/fixtures/
COPY seed.py check.py /harness/
USER 1000:1000
WORKDIR /harness
ENTRYPOINT []
