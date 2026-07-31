"""CLI wiring: transport flags, worker mode, and the stdio/HTTP split.

The defaults pinned here are scaling decisions, not preferences — stateless HTTP is what
lets any replica serve any request behind plain round-robin, and plain JSON is what keeps
ordinary proxies out of the way. A silent flip back to session mode would reintroduce
sticky-routing requirements without failing anything else.

`uvicorn.run` is patched throughout. Without that, calling `main()` starts a *real* server
and the test hangs forever instead of failing — which is exactly what happened when the
HTTP path moved from a mocked `mcp.run()` to the shared ASGI factory.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from pse_edge_mcp import __main__ as cli
from pse_edge_mcp.config import Settings


class Recorder:
    """Captures what main() hands to uvicorn / the stdio server."""

    def __init__(self) -> None:
        self.uvicorn_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.stdio_runs = 0
        self.created_settings: list[Settings] = []

    def uvicorn_run(self, *args: Any, **kwargs: Any) -> None:
        self.uvicorn_calls.append((args, kwargs))

    def create_app(self, settings: Settings | None = None) -> object:
        self.created_settings.append(settings)  # type: ignore[arg-type]
        return object()


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    rec = Recorder()
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", rec.uvicorn_run)

    import pse_edge_mcp.asgi as asgi

    monkeypatch.setattr(asgi, "create_app", rec.create_app)

    class FakeMCP:
        def run(self, *args: Any, **kwargs: Any) -> None:
            rec.stdio_runs += 1

    monkeypatch.setattr(cli, "build_server", lambda *a, **k: FakeMCP())
    # A developer's shell may carry these; these tests pin the documented defaults.
    for name in ("PSE_AUTH_REQUIRED", "PSE_STATEFUL", "PSE_SSE", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return rec


def run_cli(monkeypatch, argv: list[str]) -> None:
    monkeypatch.setattr("sys.argv", ["pse-edge-mcp", *argv])
    cli.main()


def settings_from(recorder: Recorder) -> Settings:
    assert recorder.created_settings, "create_app was not called"
    return recorder.created_settings[-1]


# --- stdio -------------------------------------------------------------------


def test_no_arguments_runs_stdio(monkeypatch, recorder):
    """The zero-config local path: stdio, no HTTP stack built at all."""
    run_cli(monkeypatch, [])
    assert recorder.stdio_runs == 1
    assert recorder.uvicorn_calls == []


def test_transport_flags_are_ignored_without_http(monkeypatch, recorder):
    """stdio stays auth-free and transport-free by principle (plan §6)."""
    monkeypatch.setenv("PSE_AUTH_REQUIRED", "1")
    run_cli(monkeypatch, ["--stateful", "--sse"])
    assert recorder.stdio_runs == 1
    assert recorder.uvicorn_calls == []


# --- HTTP transport defaults -------------------------------------------------


def test_http_is_stateless_and_json_by_default(monkeypatch, recorder):
    run_cli(monkeypatch, ["--http"])
    settings = settings_from(recorder)
    assert settings.stateful_sessions is False, "stateless default: no sticky routing"
    assert settings.sse_responses is False, "plain JSON default: no SSE for proxies to hold"


def test_stateful_opts_back_into_sessions(monkeypatch, recorder):
    run_cli(monkeypatch, ["--http", "--stateful"])
    settings = settings_from(recorder)
    assert settings.stateful_sessions is True
    assert settings.sse_responses is False, "--stateful must not silently also enable SSE"


def test_sse_is_independent_of_statefulness(monkeypatch, recorder):
    """Different concerns: statelessness is the scaling property, framing is transport."""
    run_cli(monkeypatch, ["--http", "--sse"])
    settings = settings_from(recorder)
    assert settings.sse_responses is True
    assert settings.stateful_sessions is False


def test_environment_baseline_is_not_undone_by_a_missing_flag(monkeypatch, recorder):
    """A compose file setting PSE_STATEFUL=1 must survive a command line that omits it."""
    monkeypatch.setenv("PSE_STATEFUL", "1")
    run_cli(monkeypatch, ["--http"])
    assert settings_from(recorder).stateful_sessions is True


def test_host_and_port_are_forwarded(monkeypatch, recorder):
    run_cli(monkeypatch, ["--http", "--host", "127.0.0.1", "--port", "9001"])
    _, kwargs = recorder.uvicorn_calls[0]
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9001


# --- workers -----------------------------------------------------------------


def test_single_worker_passes_the_built_app_object(monkeypatch, recorder):
    run_cli(monkeypatch, ["--http"])
    (app,), kwargs = recorder.uvicorn_calls[0]
    assert not isinstance(app, str), "one worker needs no re-import, so pass the object"
    assert "workers" not in kwargs


def test_multiple_workers_use_an_import_string(monkeypatch, recorder):
    """The supervisor forks and re-imports; an object built in main() cannot be shared."""
    run_cli(monkeypatch, ["--http", "--workers", "4"])
    (app,), kwargs = recorder.uvicorn_calls[0]
    assert app == "pse_edge_mcp.asgi:app"
    assert kwargs["workers"] == 4


def test_worker_mode_exports_transport_flags_to_the_environment(monkeypatch, recorder):
    """Workers re-import rather than re-parse argv, so flags must reach them via env."""
    run_cli(monkeypatch, ["--http", "--workers", "2", "--stateful", "--sse"])
    assert os.environ["PSE_STATEFUL"] == "1"
    assert os.environ["PSE_SSE"] == "1"


def test_uvicorn_logging_config_is_disabled_so_our_formatter_owns_output(monkeypatch, recorder):
    """Otherwise output is half JSON and half uvicorn's own prose."""
    run_cli(monkeypatch, ["--http"])
    _, kwargs = recorder.uvicorn_calls[0]
    assert kwargs["log_config"] is None


# --- help --------------------------------------------------------------------


def test_help_explains_the_consequences_of_the_flags():
    """These change deployment topology, so the help must say so — an operator reading
    `--stateful` needs to know it forces sticky routing, and `--workers` that in-memory
    limits become per worker."""
    text = cli.build_parser().format_help()
    assert "sticky" in text
    assert "stateless" in text
    assert "per worker" in text
