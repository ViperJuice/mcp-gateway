"""The operator's project `.env` must not reach the servers PMCP spawns.

Consiliency/pmcp#229, review finding S-02 (HIGH). Two sites load a dotenv file
into the gateway's own `os.environ` -- `cli.load_startup_env` (called from
`main()`, the console-script entry) and `GatewayTools._check_api_key_available`
-- and `env_store.sanitized_subprocess_env` builds every downstream server's
environment from `os.environ.copy()`. So before this fix, every key in the
operator's `.env` was inherited by every third-party server PMCP launched.

The mechanism under test is **recorded provenance, not re-derivation**: each
call site measures `set(os.environ)` before and after its own `load_dotenv` and
registers the delta, and the sanitiser strips exactly those keys. Two properties
follow, and both are pinned below because both are ways this has been got wrong:

* `load_dotenv`'s semantics are untouched -- interpolation and `override=False`
  precedence still behave exactly as they did, so the availability boolean is
  unchanged (`test_the_availability_*`). Nothing re-parses a dotenv file.
* Stripping is by *provenance*, never by *name*. A variable the operator
  exported into their shell that merely also appears in `.env` was never sourced
  from that file, so it is not in the delta and survives into the child
  (`test_a_shell_provided_variable_sharing_a_name_is_not_stripped`). Stripping
  by name would have deleted the operator's `PATH` from every spawned server.

Operator decision (A) is pinned by `test_the_servers_own_credential_still_
resolves` / `test_only_the_declared_key_is_injected`: a plain `.env` may still
supply a server's OWN declared `env_var` -- that key only, into that server only
-- because `own_env` is applied after the strip.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from pmcp import cli
from pmcp.env_store import dotenv_sourced_keys, sanitized_subprocess_env
from pmcp.tools.handlers import GatewayTools

PREFIX = "PMCP_TEST_229_"
SENTINEL = f"{PREFIX}SENTINEL"
OWN_KEY = f"{PREFIX}OWN_KEY"
OTHER_KEY = f"{PREFIX}OTHER_KEY"
QUERIED = f"{PREFIX}API_KEY"
BASE = f"{PREFIX}BASE"
DERIVED = f"{PREFIX}DERIVED"


@pytest.fixture(autouse=True)
def _drop_test_keys() -> Iterator[None]:
    """`load_dotenv` writes `os.environ` directly, so monkeypatch teardown does
    not restore it -- these tests introduce keys for real. Every key this module
    can introduce shares one prefix; drop them before and after each test."""
    for key in [k for k in os.environ if k.startswith(PREFIX)]:
        del os.environ[key]
    yield
    for key in [k for k in os.environ if k.startswith(PREFIX)]:
        del os.environ[key]


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty project directory and an empty HOME.

    Both matter: `load_startup_env` and `_check_api_key_available` read
    `Path.cwd()/.env.pmcp` and `Path.home()/.config/pmcp/pmcp.env`, and
    `managed_secret_keys` reads the same two files. Pointing them at empty
    directories keeps the assertions about the *plain* `.env` unambiguous.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(project)
    return project


def test_a_startup_env_secret_never_reaches_a_spawned_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end proof, run against production code.

    It calls `cli.load_startup_env` -- the function `main()` actually calls (see
    `test_main_uses_the_recording_startup_path`) -- not `load_dotenv` and not
    `record_dotenv_keys`. Calling `load_dotenv` here would record nothing and so
    fail even against a correct fix; calling `record_dotenv_keys` here would pass
    even if `main()` never recorded anything. Only production code proves the
    primary leak path is closed.

    The gateway may still SEE the value (it reads `.env` for its own config);
    what must not happen is the child process inheriting it.
    """
    project = _isolate(tmp_path, monkeypatch)
    (project / ".env").write_text(f"{SENTINEL}=leaked-secret\n")

    cli.load_startup_env(project / ".env")

    assert os.environ.get(SENTINEL) == "leaked-secret"
    assert SENTINEL not in sanitized_subprocess_env()


def test_main_uses_the_recording_startup_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring guard: without it, a future edit could re-inline a bare
    `load_dotenv()` into `main()` and every other test here would still pass.

    `main()` calls `parse_args()` outside its `try`, so raising `SystemExit`
    there stops the entry point right after the startup loads.
    """
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(cli, "load_startup_env", lambda *a, **kw: calls.append((a, kw)))

    def _stop() -> None:
        raise SystemExit(0)

    monkeypatch.setattr(cli, "parse_args", _stop)

    with pytest.raises(SystemExit):
        cli.main()

    assert calls == [((), {})], (
        "main() must call load_startup_env() once, with no arguments"
    )


def test_main_has_no_inline_dotenv_load() -> None:
    """The other half of the wiring guard, in the style of the #225 AST guards.

    `load_startup_env`'s `dotenv_path` parameter is a test seam: bare
    `load_dotenv()` resolves its path by walking up from the CALLING MODULE's
    directory (`find_dotenv(usecwd=False)`), which no `tmp_path` can influence.
    This asserts the production call site still passes nothing, so the default
    discovery `main()` has always used is what actually runs.
    """
    tree = ast.parse(Path(cli.__file__).read_text(), filename=cli.__file__)
    main_def = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls = [node for node in ast.walk(main_def) if isinstance(node, ast.Call)]
    called_names = {node.func.id for node in calls if isinstance(node.func, ast.Name)}
    assert "load_dotenv" not in called_names, (
        "main() must not load a dotenv file inline -- the recording lives in "
        "load_startup_env()"
    )

    startup_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "load_startup_env"
    ]
    assert len(startup_calls) == 1
    assert not startup_calls[0].args and not startup_calls[0].keywords


def test_a_shell_provided_variable_sharing_a_name_is_not_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripping by NAME would have deleted the operator's PATH from every
    spawned server: `load_dotenv` defaults to `override=False`, so a variable
    already in the environment is left alone and was never sourced from the
    file. Recording the delta is what makes the distinction."""
    project = _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "/operator/bin")
    (project / ".env").write_text(f"PATH=/dotenv/bin\n{SENTINEL}=leaked-secret\n")

    cli.load_startup_env(project / ".env")

    assert os.environ["PATH"] == "/operator/bin"
    assert "PATH" not in dotenv_sourced_keys()

    child = sanitized_subprocess_env()
    assert child["PATH"] == "/operator/bin"
    assert SENTINEL not in child


def test_the_availability_check_records_what_it_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second load site. `_check_api_key_available` answers a boolean by
    loading whole files into `os.environ`; every key it introduces is now
    recorded, so none of them reaches a child."""
    project = _isolate(tmp_path, monkeypatch)
    (project / ".env").write_text(f"{QUERIED}=key-value\n{OTHER_KEY}=other\n")

    tools = _gateway_tools()
    assert tools._check_api_key_available(QUERIED) is True

    child = sanitized_subprocess_env()
    assert QUERIED not in child
    assert OTHER_KEY not in child


def _gateway_tools() -> GatewayTools:
    """`_check_api_key_available` reads no instance state, so it needs no wired
    client/policy manager. `__new__` keeps these tests independent of
    GatewayTools' constructor; if the method ever starts using `self`, it fails
    loudly rather than silently testing something else."""
    return GatewayTools.__new__(GatewayTools)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("environ", True),
        ("dotenv", True),
        ("project_store", True),
        ("user_store", True),
        ("absent", False),
    ],
)
def test_the_availability_boolean_is_unchanged(
    source: str, expected: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """{present, absent} x {os.environ, .env, .env.pmcp, pmcp.env}.

    Characterization: every answer here was recorded against unchanged `main`
    before the fix. It stays feasible precisely because the fix records what
    `load_dotenv` did instead of re-deriving it -- nothing re-parses a file and
    no precedence rule is re-implemented.
    """
    project = _isolate(tmp_path, monkeypatch)
    home = Path(os.environ["HOME"])
    if source == "environ":
        monkeypatch.setenv(QUERIED, "v")
    elif source == "dotenv":
        (project / ".env").write_text(f"{QUERIED}=v\n")
    elif source == "project_store":
        (project / ".env.pmcp").write_text(f"{QUERIED}=v\n")
    elif source == "user_store":
        (home / ".config" / "pmcp").mkdir(parents=True)
        (home / ".config" / "pmcp" / "pmcp.env").write_text(f"{QUERIED}=v\n")

    assert _gateway_tools()._check_api_key_available(QUERIED) is expected


def test_the_availability_check_still_interpolates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_dotenv` interpolates; `read_env_file`/`dotenv_values(interpolate=
    False)` would have returned the literal `${BASE}/x`. This is why the fix
    must not re-parse the file."""
    project = _isolate(tmp_path, monkeypatch)
    (project / ".env").write_text(f"{BASE}=abc\n{DERIVED}=${{{BASE}}}/x\n")

    assert _gateway_tools()._check_api_key_available(DERIVED) is True
    assert os.environ[DERIVED] == "abc/x"
    assert DERIVED not in sanitized_subprocess_env()


def test_an_empty_value_shadows_a_later_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty value in a higher-priority file blocks a real value in a later
    one, because `load_dotenv(..., override=False)` will not replace a key that
    is already set -- even to the empty string. Per-file dicts do not reproduce
    that. Unchanged from `main`, and pinned so it stays that way."""
    project = _isolate(tmp_path, monkeypatch)
    (project / ".env").write_text(f"{QUERIED}=\n")
    (project / ".env.pmcp").write_text(f"{QUERIED}=real\n")

    assert _gateway_tools()._check_api_key_available(QUERIED) is False
    assert os.environ[QUERIED] == ""


def test_the_availability_check_short_circuits_on_no_env_var() -> None:
    assert _gateway_tools()._check_api_key_available(None) is False
    assert _gateway_tools()._check_api_key_available("") is False


def test_the_servers_own_credential_still_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator decision (A), first half: a plain `.env` may still supply a
    server's OWN declared `env_var`. Credential resolution reads it from
    `os.environ` (which still has it) and passes it as `own_env`, which is
    applied AFTER the strip."""
    project = _isolate(tmp_path, monkeypatch)
    (project / ".env").write_text(f"{OWN_KEY}=own-value\n{OTHER_KEY}=other-value\n")

    cli.load_startup_env(project / ".env")

    child = sanitized_subprocess_env({OWN_KEY: os.environ[OWN_KEY]})

    assert child[OWN_KEY] == "own-value"


def test_only_the_declared_key_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision (A), second half: that key only. The other key in the same file
    does not ride along into the same server."""
    project = _isolate(tmp_path, monkeypatch)
    (project / ".env").write_text(f"{OWN_KEY}=own-value\n{OTHER_KEY}=other-value\n")

    cli.load_startup_env(project / ".env")

    child = sanitized_subprocess_env({OWN_KEY: os.environ[OWN_KEY]})

    assert OTHER_KEY not in child
    assert os.environ[OTHER_KEY] == "other-value"  # the gateway still sees it


def test_the_registry_is_empty_without_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PMCP imported as a library, with `main()` never run, has loaded no dotenv
    file: it records nothing and therefore strips nothing extra. An empty
    registry is the correct default, not a missing initialisation."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv(SENTINEL, "from-the-operators-shell")

    assert dotenv_sourced_keys() == frozenset()
    assert sanitized_subprocess_env()[SENTINEL] == "from-the-operators-shell"
