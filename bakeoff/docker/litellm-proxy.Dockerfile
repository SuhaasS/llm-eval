# LiteLLM proxy, as a container on the eval's internal network.
#
# Not an optional deployment choice. The agent runs inside a container joined
# to an `internal=True` Docker network (spec section 5.1), which by
# construction has no route to the host -- so a proxy listening on
# 127.0.0.1:4000 is unreachable. The proxy has to be a peer on that network,
# addressed as http://litellm:4000 through Docker's embedded DNS.
#
# It also has to be a container for a second reason: the proxy is where wire
# logging happens. bakeoff.proxy_callback runs INSIDE this process, because
# this process is the only one that sees the agent's model calls.
#
# Build:
#   docker build -f docker/litellm-proxy.Dockerfile -t bakeoff-litellm .
#
# Run (mounts are what make it useful):
#   -v $PWD/src:/app/src          the callback module
#   -v <artifacts>/wire:/eval/wire  where it writes the wire log
#   -v $PWD/config:/app/config    the model list

FROM python:3.12-slim-bookworm

# Pinned to the version every claim in this repo was verified against:
# get_instance_fn's resolve-as-is behaviour, the /v1/messages route, the
# mock_response sentinels, and the exception status codes classify_exclusion
# maps. An unpinned proxy could change any of them without a diff anywhere.
ARG LITELLM_VERSION=1.95.0

# fastapi is pinned because litellm[proxy] 1.95.0 does not cap it, and 0.141.0
# removed fastapi.dependencies.utils.get_flat_dependant, which the proxy
# imports at startup. An unpinned build therefore installs a combination that
# dies with `ImportError: cannot import name 'get_flat_dependant'` before it
# serves a request -- and it would have started failing on whatever day
# 0.141.0 shipped, with nothing in this repo changed. Verified: 0.140.0 has
# the symbol, 0.141.0 does not.
ARG FASTAPI_VERSION=0.140.0

RUN pip install --no-cache-dir \
        "litellm[proxy]==${LITELLM_VERSION}" \
        "fastapi==${FASTAPI_VERSION}"

RUN installed="$(pip show litellm | awk '/^Version:/{print $2}')" \
    && [ "$installed" = "${LITELLM_VERSION}" ] \
    || { echo "expected litellm ${LITELLM_VERSION}, got ${installed}" >&2; exit 1; }

# The callback module is imported by dotted path from the proxy config, so it
# has to be importable. Mounted at run time rather than copied, so the
# harness and the proxy always run the same code.
ENV PYTHONPATH=/app/src \
    BAKEOFF_WIRE_DIR=/eval/wire

RUN mkdir -p /app/src /app/config /eval/wire
WORKDIR /app

EXPOSE 4000

ENTRYPOINT ["litellm"]
CMD ["--config", "/app/config/litellm_config.yaml", "--port", "4000", "--host", "0.0.0.0"]
