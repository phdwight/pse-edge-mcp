"""CLI wiring for the transport flags.

The defaults here are a scaling decision, not a preference, so they are pinned: stateless
HTTP is what lets any replica serve any request behind plain round-robin, and plain JSON is
what keeps ordinary proxies out of the way. A silent flip back to session mode would
reintroduce sticky-routing requirements without failing anything else.
"""

from __future__ import annotations

from typing import Any

import pytest

from pse_edge_mcp import __main__ as cli


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def run(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.fixture
def fake(monkeypatch) -> FakeServer:
    server = FakeServer()
    monkeypatch.setattr(cli, "build_server", lambda: server)
    return server


def run_cli(monkeypatch, fake: FakeServer, argv: list[str]) -> dict[str, Any]:
    # A developer's shell may carry auth env vars; these tests pin the *default* path.
    monkeypatch.delenv("PSE_AUTH_REQUIRED", raising=False)
    monkeypatch.setattr("sys.argv", ["pse-edge-mcp", *argv])
    cli.main()
    assert len(fake.calls) == 1
    return fake.calls[0][1]


def test_no_arguments_runs_stdio(monkeypatch, fake):
    """The zero-config local path: stdio, no HTTP options at all."""
    monkeypatch.setattr("sys.argv", ["pse-edge-mcp"])
    cli.main()
    args, kwargs = fake.calls[0]
    assert args == () and kwargs == {}


def test_http_is_stateless_and_json_by_default(monkeypatch, fake):
    kwargs = run_cli(monkeypatch, fake, ["--http"])
    assert kwargs["transport"] == "streamable-http"
    assert kwargs["stateless_http"] is True, "stateless is the default: no sticky routing"
    assert kwargs["json_response"] is True, "plain JSON by default: no SSE for proxies to hold"


def test_stateful_opts_back_into_sessions(monkeypatch, fake):
    kwargs = run_cli(monkeypatch, fake, ["--http", "--stateful"])
    assert kwargs["stateless_http"] is False
    assert kwargs["json_response"] is True, "--stateful must not silently also enable SSE"


def test_sse_is_independent_of_statefulness(monkeypatch, fake):
    """The two flags describe different things — statelessness is the scaling property,
    response framing is a transport detail — so they must not be coupled."""
    kwargs = run_cli(monkeypatch, fake, ["--http", "--sse"])
    assert kwargs["json_response"] is False
    assert kwargs["stateless_http"] is True


def test_both_flags_restore_full_session_mode(monkeypatch, fake):
    kwargs = run_cli(monkeypatch, fake, ["--http", "--stateful", "--sse"])
    assert kwargs["stateless_http"] is False
    assert kwargs["json_response"] is False


def test_host_and_port_are_forwarded(monkeypatch, fake):
    kwargs = run_cli(monkeypatch, fake, ["--http", "--host", "127.0.0.1", "--port", "9001"])
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9001


def test_transport_flags_are_ignored_without_http(monkeypatch, fake):
    """`--stateful` on the stdio path must not leak transport kwargs into run()."""
    monkeypatch.setattr("sys.argv", ["pse-edge-mcp", "--stateful", "--sse"])
    cli.main()
    assert fake.calls[0] == ((), {})


def test_help_explains_why_the_defaults_are_what_they_are():
    """These flags change deployment topology, so the help text has to say so — an operator
    reading `--stateful` needs to know it forces sticky routing."""
    text = cli.build_parser().format_help()
    assert "sticky" in text
    assert "stateless" in text


def test_auth_required_without_a_database_exits_with_an_actionable_message(monkeypatch, fake):
    """PSE_AUTH_REQUIRED=1 needs Postgres (accounts live there). The failure has to name
    both the missing variable and the way out, before anything starts listening."""
    monkeypatch.setenv("PSE_AUTH_REQUIRED", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["pse-edge-mcp", "--http"])

    with pytest.raises(SystemExit, match="DATABASE_URL"):
        cli.main()
    assert fake.calls == [], "nothing must start serving on a misconfigured auth setup"


def test_auth_flag_does_not_affect_stdio(monkeypatch, fake):
    """stdio stays auth-free by principle (plan §6) — even with the env var set."""
    monkeypatch.setenv("PSE_AUTH_REQUIRED", "1")
    monkeypatch.setattr("sys.argv", ["pse-edge-mcp"])
    cli.main()
    assert fake.calls[0] == ((), {})
