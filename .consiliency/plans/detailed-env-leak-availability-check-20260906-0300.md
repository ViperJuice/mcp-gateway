# Detailed plan: stop an availability *check* from loading the project `.env` into the gateway

## Task

Close Consiliency/pmcp#229 (review finding **S-02**, HIGH). `_check_api_key_available`
calls `load_dotenv()` as a side effect of answering "is this key available?".
`load_dotenv` mutates `os.environ` process-wide, so a *lookup* loads every key of
the current directory's `.env` into the gateway — and from there into every
downstream server PMCP spawns.

## Research summary

Verified against `origin/main` at `aa2de50`.

**The defect** (`src/pmcp/tools/handlers.py:2926-2947`):

```python
def _check_api_key_available(self, env_var: str | None) -> bool:
    if os.environ.get(env_var):
        return True
    for env_path in [Path.cwd()/".env", Path.cwd()/".env.pmcp",
                     Path.home()/".config"/"pmcp"/"pmcp.env"]:
        if env_path.exists():
            load_dotenv(env_path)              # <-- mutates os.environ
            if os.environ.get(env_var):
                return True
    return False
```

Note the shape: the function answers a **boolean question** and its only way of
answering is to **change global process state**. Every key in the file is loaded,
not just `env_var`. It is the sole `load_dotenv` in `handlers.py` (`:2942`); the
three in `cli.py` (`:2699-2702`) are deliberate startup configuration and are
**out of scope**.

**The leak reaches spawned servers, and the code says so.** `env_store.sanitized_subprocess_env`
(`src/pmcp/env_store.py:124`) builds a downstream server's environment from
`os.environ.copy()` minus PMCP-managed keys, and its own docstring records the
gap:

> "this removes only PMCP-managed keys; secrets the operator exported into the
> shell **or a plain `.env`** are not sanitized here."

`managed_secret_keys` (`:107`) says the same: "**not** secrets that reached
`os.environ` from the operator's shell or a plain `.env`". So keys this check
loads are precisely the keys the sanitiser does not strip. End to end: a project
`.env` containing an unrelated database URL or a third-party token is inherited
by every MCP server the gateway starts.

**The clean primitive already exists.** `env_store.read_env_file(path)`
(`:45`) parses with `dotenv_values(path, interpolate=False)` and returns a
`dict[str, str]` **without touching `os.environ`**. The fix is to answer the
question from that dict.

**Someone already knew.** `tests/test_tools.py:3586` carries the comment
"Prevent `_check_api_key_available` from loading env vars out of pmcp files on
disk" — the hazard was known well enough to work around in a test, but not
closed at the source.

**Callers** (only two): `_check_any_api_key_available` (`:2950`) and
`is_auth_available=self._check_api_key_available` passed into
`resolve_startup_configs` (`:2239`). Both want a boolean. Neither reads the
side effect.

**The load-bearing question, and it is real.** Credential *resolution* for a
manifest server runs through `_manifest_server_to_config(server, env_lookup)`
(`src/pmcp/config/loader.py:980`), and `handlers.py:200` records that it is
called with **`os.environ.get`**. So today, a credential that lives **only** in a
plain project `.env` reaches a server *because* the availability check loaded it
first. Removing the mutation removes that path. Whether that path should exist
is a **product decision**, not an implementation detail — see Decision below.

## Decision required before implementation

Three env files are consulted. They are not equivalent:

| file | owner | today | after a naive fix |
|---|---|---|---|
| `~/.config/pmcp/pmcp.env` | PMCP (`pmcp secrets set`) | loaded | must still resolve |
| `<cwd>/.env.pmcp` | PMCP, project scope | loaded | must still resolve |
| `<cwd>/.env` | **the operator's project**, not PMCP | loaded **and leaked** | ? |

The leak is the third row. The question is whether a plain `.env` stays a
*credential source* for a server's own declared `env_var`:

- **(A) Keep it, narrowly.** Resolve the specific `env_var` from `.env` and inject
  it into that server's own env only. Preserves today's behaviour for anyone
  keeping credentials in a project `.env`; closes the leak, because only the
  named key moves and only into the server that declared it.
- **(B) Drop it.** PMCP's own stores remain credential sources; a plain `.env`
  becomes lookup-only for availability, never a source. Simpler and safer, but a
  **behaviour change**: a server whose key lives only in `.env` stops receiving
  it, and `gateway.provision` may start reporting a credential as missing where
  it previously worked.

**Recommendation: (A).** It closes the reported vulnerability without removing a
working configuration path, and the narrowing — one named key, one server — is
exactly the property S-02 says is missing. (B) is defensible but should be a
deliberate deprecation with a CHANGELOG note, not a side effect of a security fix.

**This plan is written for (A) and must be re-read if the operator picks (B).**

## Changes

### `src/pmcp/tools/handlers.py` (modify)

- `_check_api_key_available` — modify — answer from `env_store.read_env_file(path)`
  instead of `load_dotenv(path)` + `os.environ.get`. Check `os.environ` first
  (unchanged fast path), then each file's parsed dict. **No process state is
  mutated.** Docstring states that the check is now side-effect free and why.
- The `load_dotenv` import (`:21`) — delete — this is its only use in the module.

### `src/pmcp/config/loader.py` (modify)

- A resolver that, given a server's declared `env_var`, returns its value from
  `os.environ` and then from the three env files, **without mutation** — the
  lookup `_manifest_server_to_config` is handed instead of bare `os.environ.get`.
  Keep it a plain `Callable[[str], str | None]` so the existing seam is unchanged;
  do **not** widen `_manifest_server_to_config`'s signature.

### `tests/test_env_leak_229.py` (create)

The proofs. Each must fail on unchanged `main`:

- `test_the_availability_check_does_not_mutate_os_environ` — write a `.env` with a
  sentinel key PMCP does not manage, ask about an **unrelated** variable, assert
  the answer is correct **and** the sentinel is absent from `os.environ`. On
  `main` the sentinel is present.
- `test_a_project_env_secret_never_reaches_a_spawned_server` — the end-to-end one:
  the same sentinel must not appear in `sanitized_subprocess_env()`. This is the
  criterion that matches the reported impact; the previous test alone would pass
  against an implementation that leaked by some other route.
- `test_the_servers_own_credential_still_resolves` — decision (A)'s other half:
  a server whose declared `env_var` lives only in `.env` still receives it.
- `test_only_the_declared_key_is_injected` — the narrowing: a *second* key in the
  same `.env` does not reach that server.
- `test_pmcp_managed_stores_still_answer` — `.env.pmcp` and `~/.config/pmcp/pmcp.env`
  still satisfy the availability check.
- `test_no_load_dotenv_outside_cli` — structural guard, in the spirit of the
  #224/#225 AST guards: `load_dotenv` may appear only in `cli.py`. Prevents the
  side effect being reintroduced anywhere else.

## Documentation impact

- `SECURITY.md` — modify — the "Credential isolation is scoped, not
  identity-complete" bullet, and the `sanitized_subprocess_env` gap it mirrors,
  must stop describing a plain `.env` as unsanitised-and-inherited once that is no
  longer true. Do not overclaim: shell-exported secrets are still inherited.
- `CHANGELOG.md` — add — `### Security`, naming the leak and the behaviour after
  the fix. `src/` changes, so the `changelog` CI job requires it.

## Dependencies & order

1. Settle the Decision. (A) and (B) produce different tests.
2. `tests/test_env_leak_229.py` first, red against `main`.
3. `_check_api_key_available` + import removal.
4. The non-mutating resolver, only if (A).
5. Docs.

## Verification

```bash
uv run pytest -q tests/test_env_leak_229.py          # the six proofs
uv run pytest -q tests/test_tools.py                  # the 2239/2950 callers
uv run pytest -q tests/                               # full suite; compare to main
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
grep -rn "load_dotenv" src/                           # expect: cli.py only
```

Host note: `/tmp/package.json` makes ~107 npm-identity tests fail here
(`tests/conftest.py`); compare the same command from the same directory
before and after, not against a clean-machine expectation.

Edge cases: no `.env` present; `.env` unreadable (must not raise — an
availability check that throws is worse than one that says "no"); the same key in
two files (first hit wins, preserving today's documented priority order); a key
whose value is empty (today falsy → "not available"; keep that).

## Acceptance criteria

- [ ] A sentinel key in `<cwd>/.env`, unrelated to the variable being asked about,
      is **absent from `os.environ`** after the availability check — where on
      unchanged `main` it is present.
- [ ] That sentinel is **absent from `sanitized_subprocess_env()`**, i.e. it never
      reaches a spawned server. This is the reported impact and the criterion that
      matters most.
- [ ] The availability check returns the **same boolean** as `main` for every
      combination of {present, absent} × {`.env`, `.env.pmcp`, `pmcp.env`,
      `os.environ`} — a fix that closes the leak by answering "no" more often is
      not a fix.
- [ ] Under decision (A): a server whose declared `env_var` exists only in `.env`
      still receives it, **and** a second key in that same file does not.
- [ ] `grep -rn "load_dotenv" src/` returns only `cli.py`, enforced by a test.
- [ ] Full-suite failures/skips/deselects unchanged from `main`; passes increase by
      exactly the new tests.

## Non-goals

- The three `load_dotenv` calls in `cli.py` — deliberate startup configuration.
- Sanitising shell-exported secrets. `sanitized_subprocess_env` inherits ambient
  environment by design; changing that is a separate decision with a much larger
  blast radius.
- The wider trust-boundary programme (#230). This fix stands alone and should not
  wait for it.

## Execution Policy

- execute: effort=medium, reason=small diff but it changes credential-resolution behaviour and the tests must prove absence rather than presence
