"""The credential window, and the cell that must not be started inside it.

Both credentials are frozen once, before the loop, into a proxy container that
lives for the whole matrix -- and the mantle bearer token is presigned with the
SigV4 session (`SigV4QueryAuth(credentials, ...)`), so it embeds
X-Amz-Security-Token and cannot outlive that session whatever its own 12 h cap
says. Measured twice, 2026-08-12 and 2026-08-13: an SSO login gives ONE HOUR on
this account. A matrix needs 4-5 days. Docker cannot change env on a running
container, so re-logging in mid-run changes nothing until the proxy restarts.

The hour is a property of the frozen copy rather than of the session: botocore
returns DeferredRefreshableCredentials, which would mint a fresh hour from the
SSO token by itself, and freezing them to literal strings for a container that
cannot re-resolve is what defeats that.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bakeoff.proxy import (
    CredentialWindow,
    credential_stop,
    credential_window,
    freeze_sigv4_credentials,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class _Frozen:
    access_key = "AKIA"
    secret_key = "secret"
    token = "session"


class _Creds:
    def __init__(self, expiry=None, raises=None):
        self._expiry_time = expiry
        self._raises = raises

    def get_frozen_credentials(self):
        if self._raises:
            raise self._raises
        return _Frozen()


def _session(creds):
    class _S:
        def __init__(self, **_kw):
            pass

        def get_credentials(self):
            return creds

    return _S


def test_an_expired_session_yields_no_credentials_rather_than_a_traceback(monkeypatch):
    """Reproduced 2026-08-12: get_frozen_credentials raises TokenRetrievalError
    on an expired SSO session, and freeze_sigv4_credentials called it unguarded
    -- so the exception escaped proxy_environment and main() as a traceback,
    bypassing the SSO_LOGIN_HINT path the function already had for
    `credentials is None`. The raise is at get_frozen_credentials, not at
    get_credentials, which is why that guard did not cover it."""
    from botocore.exceptions import TokenRetrievalError

    boom = TokenRetrievalError(provider="sso", error_msg="Token has expired")
    monkeypatch.setattr("boto3.Session", _session(_Creds(raises=boom)))

    assert freeze_sigv4_credentials("us-east-1") == {}


def test_the_window_comes_from_the_sts_expiry(monkeypatch):
    expiry = NOW + timedelta(hours=7)
    monkeypatch.setattr("boto3.Session", _session(_Creds(expiry=expiry)))

    window = credential_window("us-east-1", now=NOW, provider="bedrock")

    assert window.expires_at == expiry
    assert window.source == "sts"


def test_a_naive_expiry_is_read_as_utc(monkeypatch):
    """botocore has returned both. A naive datetime compared against an aware
    `now` raises TypeError inside credential_stop, on the path to a paid run."""
    naive = (NOW + timedelta(hours=7)).replace(tzinfo=None)
    monkeypatch.setattr("boto3.Session", _session(_Creds(expiry=naive)))

    window = credential_window("us-east-1", now=NOW, provider="bedrock")

    assert window.expires_at == NOW + timedelta(hours=7)


def test_the_window_is_capped_by_the_mantle_tokens_own_ttl(monkeypatch):
    """provide_token presigns for at most 12 h. An STS expiry beyond that does
    not extend the three mantle arms, so the window is the earlier of the two."""
    monkeypatch.setattr(
        "boto3.Session", _session(_Creds(expiry=NOW + timedelta(hours=30)))
    )

    window = credential_window("us-east-1", now=NOW, provider="bedrock")

    assert window.expires_at == NOW + timedelta(hours=12)
    assert window.source == "mantle-ttl"


def test_no_sts_expiry_still_yields_the_mantle_bound(monkeypatch):
    """Static IAM keys carry no expiry, and botocore may one day stop exposing
    the private `_expiry_time`. Neither makes the window unknown: the mantle
    token is presigned for at most 12 h, which is a real bound on three of the
    four arms. `source` says it came from the weaker half."""
    monkeypatch.setattr("boto3.Session", _session(_Creds(expiry=None)))

    window = credential_window("us-east-1", now=NOW, provider="bedrock")

    assert window.expires_at == NOW + timedelta(hours=12)
    assert window.source == "mantle-ttl"
    assert window.error


def test_a_session_that_resolves_to_nothing_is_unknown(monkeypatch):
    """The genuine unknown, and the only one. None is "nobody measured", never
    "plenty of time" -- the abort streak is the backstop here."""
    monkeypatch.setattr("boto3.Session", _session(None))

    window = credential_window("us-east-1", now=NOW, provider="bedrock")

    assert window.expires_at is None
    assert window.source == "unknown"


def test_an_already_dead_session_is_a_window_that_has_elapsed(monkeypatch):
    """Not "unknown". credential_stop must refuse the FIRST cell rather than
    spend one to discover what is already known."""
    from botocore.exceptions import TokenRetrievalError

    monkeypatch.setattr("boto3.Session", _session(
        _Creds(raises=TokenRetrievalError(provider="sso", error_msg="expired"))
    ))

    window = credential_window("us-east-1", now=NOW, provider="bedrock")

    assert window.source == "expired"
    assert credential_stop(window, now=NOW, needed_s=1)


def test_a_cell_that_cannot_finish_before_expiry_is_not_started():
    """The margin is the task's own wall_clock_timeout_s plus the driver's
    per-cell overhead, which is why this needs no estimate of how long a matrix
    takes: a cell is refused exactly when it could outlive the credentials that
    have to serve it."""
    window = CredentialWindow(expires_at=NOW + timedelta(minutes=10), source="sts")

    reason = credential_stop(window, now=NOW, needed_s=900)

    assert reason
    assert "expire" in reason


def test_a_cell_that_fits_inside_the_window_runs():
    window = CredentialWindow(expires_at=NOW + timedelta(hours=3), source="sts")

    assert credential_stop(window, now=NOW, needed_s=900) == ""


def test_the_boundary_stops_rather_than_starts():
    """`remaining == needed_s` is a cell that finishes at the exact instant the
    credentials die. Conservative on purpose: the cost of stopping one cell
    early is a re-invocation, and the cost of starting it is a consumed run_id."""
    window = CredentialWindow(expires_at=NOW + timedelta(seconds=900), source="sts")

    assert credential_stop(window, now=NOW, needed_s=900)


def test_an_unknown_window_never_blocks_a_cell():
    """Refusing to run on an unreadable expiry would turn a diagnostic into an
    outage. The abort streak already contains the damage to a few cells."""
    assert credential_stop(CredentialWindow(None, "unknown"), now=NOW, needed_s=900) == ""


def test_the_caller_is_what_adds_the_cell_overhead():
    """credential_stop is pure and knows nothing about the driver. The margin
    that actually protects a cell is wall_clock_timeout_s + CELL_OVERHEAD_S,
    applied at the call site -- pinned here so the two cannot drift apart
    silently."""
    from scripts.run_matrix import CELL_OVERHEAD_S

    window = CredentialWindow(
        expires_at=NOW + timedelta(seconds=900 + CELL_OVERHEAD_S - 1), source="sts"
    )

    assert credential_stop(window, now=NOW, needed_s=900) == ""
    assert credential_stop(window, now=NOW, needed_s=900 + CELL_OVERHEAD_S)


# --------------------------------------------------------------- providers

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bakeoff.proxy import (
    DEFAULT_PROVIDER,
    EVAL_ARMS_BY_PROVIDER,
    OPENROUTER_KEY_ENV,
    PROVIDER_ENV,
    PROVIDERS,
    proxy_environment,
)

BAKEOFF = Path(__file__).resolve().parents[1]


def test_openrouter_is_the_default_provider():
    assert DEFAULT_PROVIDER == "openrouter"
    assert set(PROVIDERS) == {"openrouter", "bedrock"}
    assert EVAL_ARMS_BY_PROVIDER["openrouter"] == ["kimi-k2-6", "kimi-k3"]
    assert EVAL_ARMS_BY_PROVIDER["bedrock"][0] == "claude-sonnet-5-runtime"


def test_the_openrouter_environment_is_exactly_the_key_and_the_provider_name(monkeypatch):
    """Spec §1. Two keys, no AWS names: one proxy image serves both providers,
    and an AWS variable leaking into an openrouter proxy is how a misrouted
    deployment would authenticate against the wrong cloud."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, "sk-or-test")
    env = proxy_environment("live", "openrouter")
    assert env == {OPENROUTER_KEY_ENV: "sk-or-test", PROVIDER_ENV: "openrouter"}


def test_offline_mode_hands_the_proxy_nothing_for_either_provider():
    assert proxy_environment("offline", "openrouter") == {}
    assert proxy_environment("offline", "bedrock") == {}


def test_a_missing_openrouter_key_refuses_before_the_proxy_is_built(monkeypatch):
    monkeypatch.delenv(OPENROUTER_KEY_ENV, raising=False)
    # Point the .env lookup at an empty directory so the operator's real
    # key cannot make this test pass on one machine and fail on another.
    monkeypatch.setattr("bakeoff.proxy.ENV_FILE", Path("/nonexistent/.env"))
    with pytest.raises(SystemExit) as excinfo:
        proxy_environment("live", "openrouter")
    assert OPENROUTER_KEY_ENV in str(excinfo.value)


def test_an_unknown_provider_is_a_programming_error_not_a_fallback():
    with pytest.raises(ValueError):
        proxy_environment("live", "azure")


def test_the_openrouter_path_never_loads_botocore_or_the_bedrock_preflight():
    """Spec §1, §8. Asserted in a fresh interpreter: the bedrock branch's
    imports are module-level in scripts/smoke_bedrock.py, and a shared
    import would drag botocore into a run that has no AWS credentials and no
    reason to resolve any."""
    code = (
        "import sys\n"
        "from bakeoff.proxy import proxy_environment\n"
        "proxy_environment('live', 'openrouter')\n"
        "leaked = [m for m in ('botocore', 'boto3', 'scripts.smoke_bedrock', 'smoke_bedrock') if m in sys.modules]\n"
        "assert not leaked, leaked\n"
    )
    env = {**os.environ, OPENROUTER_KEY_ENV: "sk-or-test", "PYTHONPATH": str(BAKEOFF / "src")}
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=BAKEOFF
    )
    assert result.returncode == 0, result.stderr


def test_the_openrouter_window_has_no_expiry_and_never_stops_a_cell():
    """A static key has no deadline to read. `None` here is 'nothing to
    read', distinguished from botocore's 'could not read' by `source`, and
    credential_stop must not refuse a cell over it -- the abort streak is the
    backstop for a revoked key, as it already is for static AWS keys."""
    window = credential_window("us-east-1", now=NOW, provider="openrouter")
    assert window.expires_at is None
    assert window.source == "openrouter-static"
    assert credential_stop(window, NOW, 10_000) == ""


def test_the_env_template_names_the_openrouter_key_first():
    template = (BAKEOFF / ".env.example").read_text()
    keys = [
        line.split("=", 1)[0].strip()
        for line in template.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    assert keys[0] == OPENROUTER_KEY_ENV, keys
