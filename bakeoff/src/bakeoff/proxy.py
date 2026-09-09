"""The LiteLLM proxy container and the credentials it needs.

Extracted from `scripts/smoke_test.py` unchanged. The extraction is not
tidiness: this module holds the eval's topology -- two networks, which arm
rides which transport, and which environment variable carries which
credential -- and while it lived in a script, the only way to run a real
matrix was to reimplement it. Two implementations of "the eval's
environment" means the Phase 0c gate certifies one thing and the paid run
executes another, at which point the gate gates nothing.

Everything here is verified against the runs recorded in TASKS.md; the
comments carry the failures each line was written for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The two provider routes behind the proxy (spec 2026-09-08 §1). Selected by
# `--provider` on the drivers, never inferred from which env var happens to be
# set: an implicit choice is configuration reported as observation.
PROVIDERS = ("openrouter", "bedrock")
DEFAULT_PROVIDER = "openrouter"
# How the PROXY learns which provider it serves. Read by litellm_patches to
# decide whether the two mantle-only rewrites apply, and written into the
# adapter manifest so the record can say which route answered.
PROVIDER_ENV = "BAKEOFF_PROVIDER"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

# The arms each provider's config serves. `run_matrix --models` defaults to the
# list for the chosen provider; a bedrock arm name against an openrouter config
# is an UnknownModelError after the tokens are spent.
#
# The bedrock four are NOT one transport: Sonnet 5 runs on bedrock-runtime and
# the three candidates on bedrock-mantle.
#
# That split is not a preference. The mantle passthrough derives
# `anthropic-beta` HTTP headers from Claude Code's context_management and
# output_config and mantle rejects them, so the mantle Sonnet arm 400s on
# every call with "invalid beta flag" -- measured twice on 2026-08-07, and
# additional_drop_params cannot reach a header. bedrock-runtime carries beta
# values as a request-body field instead and completes the task.
#
# The cost is a section 6.4 confound on the reference arm, tracked in
# TASKS.md: Sonnet is not transport-identical to the arms it is the reference
# for. `claude-sonnet-5` stays reachable through --models as the control that
# demonstrates the difference, but it does not earn a slot in the default set.
#
# Defined here rather than in either caller. The Phase 0c gate and the matrix
# driver disagreeing about what "the four arms" are is the same class of
# defect this module exists to prevent, one level down.
EVAL_ARMS_BY_PROVIDER: dict[str, list[str]] = {
    "bedrock": [
        "claude-sonnet-5-runtime",
        "gemma-4-31b",
        "nemotron-3-super-120b",
        "kimi-k2-5",
    ],
    "openrouter": ["kimi-k2-6", "kimi-k3"],
}
SSO_LOGIN_HINT = (
    "  AWS_CONFIG_FILE=bakeoff/.aws/config aws sso login --profile pindrop-bakeoff"
)
OPENROUTER_KEY_HINT = (
    f"  put {OPENROUTER_KEY_ENV}=<key> in bakeoff/.env (see .env.example, section 0)"
)

# src/bakeoff/proxy.py -> bakeoff/.env. The same file scripts/smoke_bedrock.py
# reads; resolved here so the openrouter branch needs nothing from that script.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Proxy:
    """The LiteLLM proxy container, on two networks.

    The agent must be isolated (section 5.1) and the proxy must reach
    Bedrock. Those are incompatible on one network: `internal=True` removes
    the external route by definition. So the agent joins the internal
    network only, and the proxy joins both -- it is the recording hop, and
    the only thing on the eval network with a way out.

        agent --[internal]-- proxy --[egress]-- Bedrock

    Names are unique per invocation. A leftover `litellm` container from a
    crashed integration run would otherwise satisfy the healthcheck and
    answer with the wrong config.

    `image` and `repo_root` are parameters rather than module constants so
    the same class serves the smoke gate and the matrix driver. Nothing else
    about the topology is adjustable, deliberately.
    """

    def __init__(
        self,
        config_name: str,
        wire_dir: Path,
        env: dict[str, str],
        tag: str,
        image: str,
        repo_root: Path,
        with_stub: bool = False,
    ):
        self.config_name = config_name
        self.wire_dir = wire_dir
        self.env = env
        self.tag = tag
        self.image = image
        self.repo_root = Path(repo_root)
        self.with_stub = with_stub
        self.stub = None
        self.internal_name = f"bakeoff-smoke-internal-{tag}"
        self.egress_name = f"bakeoff-smoke-egress-{tag}"
        self.container = None
        self.internal = None
        self.egress = None

    def __enter__(self):
        import docker

        client = docker.from_env()
        self.wire_dir.mkdir(parents=True, exist_ok=True)
        self.internal = client.networks.create(
            self.internal_name, driver="bridge", internal=True
        )
        self.egress = client.networks.create(self.egress_name, driver="bridge")
        if self.with_stub:
            # On the internal network only. It stands in for Bedrock, so if
            # it could be reached any other way the offline gate would stop
            # proving that the agent's only route is through the proxy.
            self.stub = client.containers.run(
                self.image,
                entrypoint=["python", "/app/fixtures/anthropic_stub.py"],
                command=[],
                name=f"stub-{self.tag}",
                network=self.internal_name,
                volumes={
                    str(self.repo_root / "fixtures"): {
                        "bind": "/app/fixtures",
                        "mode": "ro",
                    }
                },
                detach=True,
            )
            self.internal.disconnect(self.stub)
            self.internal.connect(self.stub, aliases=["stub"])
        self.container = client.containers.run(
            self.image,
            command=[
                "--config",
                f"/app/config/{self.config_name}",
                "--port",
                "4000",
                "--host",
                "0.0.0.0",
            ],
            # The hostname the agent resolves through Docker's embedded DNS.
            # base_url is http://litellm:4000 for exactly this reason: an
            # internal network has no host route, so 127.0.0.1 is the
            # agent's own container.
            name=f"litellm-{self.tag}",
            hostname="litellm",
            network=self.internal_name,
            environment=self.env,
            volumes={
                str(self.repo_root / "src"): {"bind": "/app/src", "mode": "ro"},
                str(self.repo_root / "config"): {"bind": "/app/config", "mode": "ro"},
                str(self.wire_dir): {"bind": "/eval/wire", "mode": "rw"},
            },
            detach=True,
        )
        # Aliased so the name stays `litellm` on the internal network even
        # though the container name is unique.
        self.internal.disconnect(self.container)
        self.internal.connect(self.container, aliases=["litellm"])
        self.egress.connect(self.container)
        self._wait()
        return self

    def _wait(self, timeout_s: int = 120) -> None:
        """Probe from INSIDE the container: the internal network has no host
        route, so there is nothing to curl from here."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.container.reload()
            if self.container.status != "running":
                raise RuntimeError(
                    "proxy exited before serving:\n"
                    + self.container.logs(tail=40).decode("utf-8", "replace")
                )
            probe = self.container.exec_run(
                [
                    "python",
                    "-c",
                    "import urllib.request;"
                    "urllib.request.urlopen("
                    "'http://127.0.0.1:4000/health/liveliness', timeout=2)",
                ]
            )
            if probe.exit_code == 0:
                return
            time.sleep(2)
        raise RuntimeError(
            "proxy did not become ready:\n"
            + self.container.logs(tail=40).decode("utf-8", "replace")
        )

    def logs(self, tail: int = 40) -> str:
        if self.container is None:
            return ""
        return self.container.logs(tail=tail).decode("utf-8", "replace")

    def request_count(self) -> int:
        """Client requests the proxy served, counted from its own log.

        The independent check on capture: every call the proxy answered must
        appear in the wire log or in unattributed.jsonl. A call that lands in
        neither is silently missing evidence, and section 6.2 makes the wire
        log the record of what went over the wire -- an incomplete one is
        worse than an absent one, because it looks complete.

        This counts INBOUND requests, which is not the same quantity as the
        wire log's entries: `num_retries: 3` means one request can produce
        several provider attempts, each its own callback invocation. The
        reconciliation in the caller is what reads the two against each other.

        Untruncated on purpose. `tail=10000` silently capped the numerator on
        a long run, so a matrix big enough to matter would under-report served
        and the check would pass by losing evidence of losing evidence.
        """
        return self.logs(tail="all").count("POST /v1/messages")

    def __exit__(self, *_exc):
        # Kept unconditionally: on a teardown after a failure this is the
        # only account of what the proxy saw.
        try:
            (self.wire_dir.parent / "proxy.log").write_text(self.logs(tail=10000))
        except Exception:  # noqa: BLE001
            pass
        # Containers first, and networks only after. A network with an
        # attached container refuses to go, and Network.remove() takes no
        # `force` -- passing one raises TypeError, which a bare except then
        # swallows, leaking a network per invocation until the daemon runs
        # out of address space.
        for container in (self.container, self.stub):
            if container is None:
                continue
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001 - teardown must not mask a failure
                pass
        for network in (self.internal, self.egress):
            if network is None:
                continue
            for _ in range(10):
                try:
                    network.remove()
                    break
                except Exception:  # noqa: BLE001 - container removal is async
                    time.sleep(1)
        return False


def freeze_sigv4_credentials(region: str) -> dict[str, str]:
    """Resolve the ambient AWS session into literal keys for the container.

    The bedrock-runtime arms sign SigV4, and the proxy container cannot
    resolve an SSO profile itself: it has no ~/.aws, no SSO cache and no
    browser to re-authenticate with. Freezing on the host and passing the
    triple in is the only way those arms reach Bedrock at all.

    These are temporary STS credentials and inherit the SSO session's expiry,
    the same hours-not-days horizon as the mantle token -- the unattended
    multi-day run in TASKS.md P1 needs a refresh path that does not exist yet,
    for both transports rather than just one.
    """
    import boto3

    session = boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        return {}
    try:
        frozen = credentials.get_frozen_credentials()
    except Exception as exc:  # noqa: BLE001 - expired-session guard, see below
        # Reproduced 2026-08-12: an expired SSO session raises
        # TokenRetrievalError HERE and not at get_credentials, so the guard
        # above did not cover it and the traceback escaped proxy_environment
        # and main(). The caller already turns an empty dict into the
        # SSO_LOGIN_HINT message.
        print(f"sigv4     could not freeze the session: {type(exc).__name__}: {exc}")
        return {}
    env = {
        "AWS_ACCESS_KEY_ID": frozen.access_key,
        "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
        # Region twice on purpose: LiteLLM reads AWS_REGION_NAME, botocore
        # reads AWS_DEFAULT_REGION, and a deployment without aws_region_name
        # would otherwise sign against a region it guessed.
        "AWS_REGION_NAME": region,
        "AWS_DEFAULT_REGION": region,
    }
    if frozen.token:
        env["AWS_SESSION_TOKEN"] = frozen.token
    return env


# provide_token's own ceiling (aws_bedrock_token_generator: "less than or equal
# to 12 hours"). A CAP and not a floor: the token is a presigned SigV4 over the
# session credentials, so it carries X-Amz-Security-Token and dies with the
# session whatever this says.
MANTLE_TOKEN_TTL = timedelta(hours=12)


@dataclass(frozen=True)
class CredentialWindow:
    """How long the frozen credentials remain usable, or that nobody knows.

    `expires_at is None` means UNDETERMINED and never "plenty of time" -- the
    same rule the record follows for `isolated` and `cache_state.warm`. A
    matrix must not be blocked by an unreadable expiry, so `credential_stop`
    returns "" for it and the abort streak stays the backstop.

    An already-dead session is NOT this state: it is an elapsed window, so the
    first cell is refused rather than spent discovering what is already known.
    """

    expires_at: datetime | None
    source: str
    error: str = ""


def credential_window(
    region: str, now: datetime | None = None, *, provider: str
) -> CredentialWindow:
    """When the credentials handed to the proxy stop working.

    Both transports die at the same instant and that is not a coincidence:
    `derive_mantle_token` presigns with the SigV4 session, so the bearer token
    embeds that session's token.

    ONE HOUR, measured twice: a login at 2026-08-12T23:54Z expired at
    2026-08-13T00:54:38Z, and one at 2026-08-13T06:29Z at 07:29:35Z. That is
    the Identity Center session policy on this account, and MANTLE_TOKEN_TTL's
    12 h therefore never binds.

    The hour is a property of the FROZEN COPY, not of the session. botocore
    hands back DeferredRefreshableCredentials, which would mint a fresh hour
    from the SSO token on its own; `freeze_sigv4_credentials` resolves them to
    literal strings for a container that cannot re-resolve, and that is exactly
    what defeats the refresh. Which is why the deadline has to be checked
    rather than assumed away.

    MUST be called after `proxy_environment`, which is what runs
    `resolve_aws_paths` and therefore points AWS_CONFIG_FILE at the
    project-local profile. Called before it, this reads the operator's ambient
    session -- a different credential from the one the proxy holds.

    OPENROUTER. The key is static and the window is `None` with source
    `openrouter-static` -- see the branch below. Everything under this line is
    the bedrock provider.

    `_expiry_time` is botocore-private (botocore 1.x, RefreshableCredentials).
    Guarded with getattr and a fallback rather than trusted: an attribute that
    disappears must yield a weaker bound, not a crash on the path to a paid run.
    """
    if provider == "openrouter":
        # A static API key. There is no deadline to read, which is a
        # different state from "could not read one": `source` says so, and
        # credential_stop returns "" for it. The abort streak is the backstop
        # for a revoked or exhausted key, as it already is for static AWS keys
        # (CLAUDE.md, "That refusal cannot fire on static keys").
        return CredentialWindow(None, "openrouter-static", "static API key; no expiry to read")

    now = now or datetime.now(timezone.utc)
    try:
        import boto3

        credentials = boto3.Session(region_name=region).get_credentials()
    except Exception as exc:  # noqa: BLE001 - a probe must not cost the run
        return CredentialWindow(None, "unknown", f"{type(exc).__name__}: {exc}")
    if credentials is None:
        return CredentialWindow(None, "unknown", "no credentials resolved")

    try:
        credentials.get_frozen_credentials()
    except Exception as exc:  # noqa: BLE001
        # Already dead. An ELAPSED window, not an unknown one, so the first
        # cell is refused instead of spent finding out.
        return CredentialWindow(now, "expired", f"{type(exc).__name__}: {exc}")

    # Measured from `now` because provide_token presigns at mint time and
    # run_matrix calls this immediately after proxy_environment mints it. The
    # seconds between them are inside any margin worth acting on.
    mantle_deadline = now + MANTLE_TOKEN_TTL

    expiry = getattr(credentials, "_expiry_time", None)
    if not isinstance(expiry, datetime):
        # Not unknown. Static IAM keys have no expiry and botocore may stop
        # exposing the private attribute, but the mantle token's own 12 h
        # presign still bounds three of the four arms -- so this is a real
        # window from the weaker half, with `error` naming the missing half.
        return CredentialWindow(
            mantle_deadline,
            "mantle-ttl",
            "no STS expiry available; bounded by the mantle token alone",
        )
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if mantle_deadline < expiry:
        return CredentialWindow(mantle_deadline, "mantle-ttl")
    return CredentialWindow(expiry, "sts")


def credential_stop(window: CredentialWindow, now: datetime, needed_s: int) -> str:
    """"" to run the next cell, or why it must not be started.

    `needed_s` is the CELL's budget, not the agent's: the caller adds
    run_matrix.CELL_OVERHEAD_S to the task's wall_clock_timeout_s, because
    materializing a tree, starting a container, the final force_capture and the
    record write all sit outside the agent's cap. This is why the check needs
    no estimate of how long a whole matrix takes -- a cell is refused exactly
    when it could outlive the credentials that have to serve it.

    Starting one anyway spends the tokens and then writes a row that measures
    nothing, and `run_id` is deterministic under mode "x", so that cell is
    consumed forever.

    Boundary is deliberately conservative: `remaining == needed_s` stops.
    """
    if window.expires_at is None:
        return ""
    remaining = (window.expires_at - now).total_seconds()
    if remaining > needed_s:
        return ""
    return (
        f"credentials expire at {window.expires_at.isoformat()} "
        f"({int(remaining)}s away, source {window.source}); the next cell is "
        f"allowed {needed_s}s and could not finish before then"
    )


def proxy_environment(mode: str, provider: str) -> dict[str, str]:
    """Credentials for the proxy container. Live mode only.

    The agent is passed none of these and never sees them: it reaches the
    provider through the proxy over HTTP, which is what makes every credential
    here a single-hop secret.

    Two providers, two bodies, no shared code (spec 2026-09-08 §1). The
    openrouter branch must not import the bedrock preflight or botocore: a
    path that needs no AWS credential must not resolve one, and the test
    asserts it on `sys.modules` in a fresh interpreter.

    Every return carries PROVIDER_ENV. That is how the proxy process learns
    which rewrites to apply (litellm_patches) and what to write into the
    adapter manifest (`provider_route` on the record).
    """
    if mode != "live":
        return {}
    if provider == "openrouter":
        return _openrouter_environment()
    if provider == "bedrock":
        return {**_bedrock_environment(), PROVIDER_ENV: "bedrock"}
    raise ValueError(f"unknown provider {provider!r}; expected one of {PROVIDERS}")


def _openrouter_environment() -> dict[str, str]:
    import os

    from bakeoff.envfile import load_env_file

    load_env_file(ENV_FILE)
    key = os.environ.get(OPENROUTER_KEY_ENV, "")
    if not key:
        raise SystemExit(f"No {OPENROUTER_KEY_ENV}. Run:\n" + OPENROUTER_KEY_HINT)
    return {OPENROUTER_KEY_ENV: key, PROVIDER_ENV: "openrouter"}


def _bedrock_environment() -> dict[str, str]:
    """Both bedrock transports are provisioned, because Sonnet 5 is served over
    bedrock-runtime while the three candidates are served over bedrock-mantle
    (see config/litellm_config.yaml). The mantle token is passed as
    BAKEOFF_MANTLE_TOKEN and NOT as AWS_BEARER_TOKEN_BEDROCK: LiteLLM's
    bedrock/ handler falls back to the AWS-named variable when a deployment
    has no api_key, so setting it here would bearer-authenticate every runtime
    arm and fail them with `bedrock:CallWithBearerToken`.
    """
    import os

    from scripts.smoke_bedrock import (
        ENV_FILE as BEDROCK_ENV_FILE,
        LITELLM_BEARER_ENV,
        MANTLE_ENV,
        derive_mantle_token,
        load_env_file,
        normalize_mantle_token,
        resolve_aws_paths,
        scrub_placeholders,
    )

    load_env_file(BEDROCK_ENV_FILE)
    scrub_placeholders()
    resolve_aws_paths()
    normalize_mantle_token()

    region = os.environ.get("AWS_REGION_NAME") or "us-east-1"

    token = os.environ.get(MANTLE_ENV, "")
    if not token:
        # Minted from the current SSO session, in memory, never written to
        # disk. A long-lived key in .env would outlive the run that needed
        # it and sit there with no expiry anyone tracks.
        token = derive_mantle_token(region)
        if token:
            print(f"mantle    derived short-term bearer token (len {len(token)})")
    if not token:
        raise SystemExit("No mantle credential. Run:\n" + SSO_LOGIN_HINT)

    sigv4 = freeze_sigv4_credentials(region)
    if not sigv4:
        raise SystemExit(
            "No SigV4 credentials -- every bedrock-runtime arm would fail.\n"
            + SSO_LOGIN_HINT
        )
    print(f"sigv4     froze session credentials for the proxy ({region})")

    environment = {MANTLE_ENV: token, **sigv4}
    # Belt and braces: the container inherits nothing from this process, but
    # an image or compose layer that ever sets the AWS-named variable would
    # silently move the runtime arms onto bearer auth.
    assert LITELLM_BEARER_ENV not in environment
    return environment
