"""Shared PMCP credential env-file storage helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from dotenv import dotenv_values

from pmcp.config.loader import find_project_root

ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_env_var_name(name: str) -> str:
    """Validate and return a shell-compatible env var name."""
    if not ENV_VAR_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Env var name must match ^[A-Za-z_][A-Za-z0-9_]*$: {name!r}")
    return name


def resolve_project_root(project: Path | None = None) -> Path:
    """Resolve project root for project-scope secrets."""
    if project:
        return project.resolve()

    discovered = find_project_root(Path.cwd())
    if discovered:
        return discovered

    return Path.cwd().resolve()


def resolve_scope_path(scope: str, project: Path | None = None) -> Path:
    """Resolve env file path for a credential scope."""
    if scope == "user":
        return Path.home() / ".config" / "pmcp" / "pmcp.env"
    if scope == "project":
        return resolve_project_root(project) / ".env.pmcp"
    raise ValueError(f"Unsupported secret scope: {scope}")


def read_env_file(path: Path) -> dict[str, str]:
    """Read .env key/value pairs from path."""
    if not path.exists():
        return {}

    parsed = dotenv_values(path, interpolate=False)
    values: dict[str, str] = {}
    for key, value in parsed.items():
        if value is None:
            values[key] = ""
        else:
            values[key] = value
    return values


def _validate_env_values(values: dict[str, str]) -> None:
    for key, value in values.items():
        validate_env_var_name(key)
        if "\n" in value or "\r" in value:
            raise ValueError("Credential values must not contain newlines")


def _format_env_value(value: str) -> str:
    if value == "":
        return '""'

    needs_quotes = any(ch.isspace() for ch in value) or any(
        ch in value for ch in ["#", "=", '"', "'", "\\"]
    )
    if not needs_quotes:
        return value

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write key/value pairs to .env file and lock permissions to 0600."""
    _validate_env_values(values)

    lines = [f"{key}={_format_env_value(val)}" for key, val in values.items()]
    content = "\n".join(lines)
    if content:
        content += "\n"

    # Tighten only directories PMCP itself creates (e.g. ~/.config/pmcp for
    # user-scope secrets) to 0700. Never chmod a pre-existing directory such as
    # a project root, which for project-scope secrets is path.parent.
    parent = path.parent
    parent_created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if parent_created:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as env_file:
        env_file.write(content)


# Env-var keys PMCP itself introduced into its OWN environment from a dotenv
# file. Provenance, not file contents: see record_dotenv_keys (Consiliency/pmcp#229).
_DOTENV_SOURCED_KEYS: set[str] = set()


def record_dotenv_keys(keys: Iterable[str]) -> None:
    """Record env-var keys that PMCP itself introduced from a dotenv file.

    Callers record the delta they measured around their own ``load_dotenv``
    call -- ``set(os.environ)`` after, minus before -- so this registry holds
    *provenance*, never file contents, and never a re-parse:

    .. code-block:: python

        before = set(os.environ)
        load_dotenv(...)                       # semantics untouched
        record_dotenv_keys(set(os.environ) - before)

    Recording rather than re-deriving is what makes the strip correct.
    ``load_dotenv`` defaults to ``override=False``, so a variable the operator
    exported into their shell is left alone even when a dotenv file names it
    too; such a variable is already in ``before``, so it is never recorded and
    never stripped. Stripping by key name instead would delete the operator's
    ``PATH`` from every spawned server. Interpolation and empty-value shadowing
    need no emulation either -- whatever ``load_dotenv`` did is what is recorded.

    Additive and idempotent. An empty registry is the correct default: PMCP
    imported as a library, with ``main()`` never run, has loaded no dotenv file
    and so strips nothing extra.
    """
    _DOTENV_SOURCED_KEYS.update(keys)


def dotenv_sourced_keys() -> frozenset[str]:
    """Keys PMCP introduced into its own environment from dotenv files."""
    return frozenset(_DOTENV_SOURCED_KEYS)


def reset_dotenv_keys() -> None:
    """Clear the dotenv provenance registry. **Test-only seam.**

    Production never calls this: the registry only grows, as startup and
    availability-check loads happen. Tests need it because the registry is
    process-global -- without a reset, keys one test recorded would be stripped
    from every later test's subprocess environment. An autouse fixture in
    ``tests/conftest.py`` calls it around every test.
    """
    _DOTENV_SOURCED_KEYS.clear()


def managed_secret_keys(project: Path | None = None) -> set[str]:
    """Env-var keys of credentials PMCP manages in its user/project secret stores.

    These are the keys ``auth_connect`` / ``pmcp secrets set`` write (and that the
    gateway loads into its own ``os.environ`` at startup). Used to avoid bleeding
    one server's PMCP-stored credentials into another server's subprocess env.
    Only PMCP-managed keys are enumerated — not secrets that reached ``os.environ``
    from the operator's shell or a plain ``.env``.
    """
    keys: set[str] = set(read_env_file(resolve_scope_path("user")))
    try:
        keys.update(read_env_file(resolve_scope_path("project", project)))
    except (OSError, ValueError):
        pass
    return keys


def sanitized_subprocess_env(
    own_env: Mapping[str, str] | None = None, project: Path | None = None
) -> dict[str, str]:
    """Build the environment for a downstream server subprocess.

    Inherits the gateway's environment MINUS PMCP-managed credentials — so a
    server never receives ANOTHER server's PMCP-stored secrets — then applies the
    server's OWN resolved credentials (``own_env``), which win over the strip
    (e.g. a server whose runtime env_var equals a managed key gets its own value
    back). Non-secret ambient vars (PATH/HOME/NODE_*/proxy/locale) are preserved.

    Also stripped: keys PMCP introduced into its own environment from a dotenv
    file (``dotenv_sourced_keys``) -- the operator's project ``.env`` reached
    the gateway through ``cli.load_startup_env`` and the availability check, and
    was inherited by every server PMCP spawned (Consiliency/pmcp#229). Those keys
    are stripped by recorded provenance, not by name, so a shell-exported
    variable that merely shares a name with a ``.env`` entry survives. A server's
    OWN declared ``env_var`` still arrives via ``own_env``, which is applied
    after the strip.

    Note: secrets the operator exported into their shell are still inherited --
    deliberately, and out of scope here.
    """
    env = os.environ.copy()
    for key in managed_secret_keys(project):
        env.pop(key, None)
    for key in dotenv_sourced_keys():
        env.pop(key, None)
    if own_env:
        env.update(own_env)
    return env


def set_env_value(
    scope: str, key: str, value: str, project: Path | None = None
) -> Path:
    """Set one env value in user or project PMCP credential storage."""
    validate_env_var_name(key)
    if "\n" in value or "\r" in value:
        raise ValueError("Credential values must not contain newlines")

    path = resolve_scope_path(scope, project)
    values = read_env_file(path)
    values[key] = value
    write_env_file(path, values)
    return path
