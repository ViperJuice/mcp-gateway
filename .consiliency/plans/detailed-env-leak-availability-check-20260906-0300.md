# Detailed plan: stop the project `.env` reaching downstream servers

> **Revision 2 (2026-09-06).** Rev 1 boarded 1 AGREE / 1 PARTIALLY AGREE / 1
> DISAGREE. Four defects, all verified against source; one is a **scope error
> that would have shipped a fix which did not close the leak**.
>
> 1. **`cli.py:2699` is the primary leak path, and rev 1 declared it out of
>    scope.** `main()` — the gateway entry point — calls bare `load_dotenv()`,
>    which loads the project `.env` into `os.environ` at startup,
>    unconditionally, before any availability check runs. Fixing only
>    `_check_api_key_available` would have closed a side door, left the front
>    door open, and then updated SECURITY.md to say the leak was closed. The
>    centre of gravity moves accordingly (see Changes).
> 2. **The resolver was never wired.** Rev 1 put the non-mutating resolver in
>    `loader.py` but did not list the `handlers.py` call site that actually
>    passes `os.environ.get` — so decision (A) would not have worked and
>    `test_the_servers_own_credential_still_resolves` would have failed. Found
>    independently by two seats.
> 3. **`interpolate=False` is not equivalent to `load_dotenv`.** Measured:
>    for `DERIVED=${BASE}/x`, `dotenv_values(..., interpolate=False)` yields the
>    literal `${BASE}/x` while `load_dotenv` yields `abc/x`. Swapping one for the
>    other silently changes values, which the "same boolean as main" criterion
>    would not necessarily catch.
> 4. **Removing the import breaks an existing test.** `tests/test_tools.py:3587`
>    does `monkeypatch.setattr("pmcp.tools.handlers.load_dotenv", ...)`; deleting
>    the import makes that raise `AttributeError`.
>
> Also carried in: `read_env_file` must be guarded against `OSError` /
> `PermissionError` on an existing-but-unreadable file, and empty-value priority
> differs between the two approaches (an empty higher-priority value blocks a
> later file under `load_dotenv`'s no-override semantics, but not under
> per-file dicts).


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

### `src/pmcp/env_store.py` (modify) — the load-bearing change

**The leak is closed at the boundary that owns it.** The gateway legitimately
loads `.env` for its own configuration (`cli.py:2699`); what must not happen is
that those keys are inherited by third-party downstream servers. So
`sanitized_subprocess_env` — which already exists to strip PMCP-managed keys and
already documents this exact gap — also strips keys sourced from the **project
`.env`**, except the server's own declared `env_var` (decision (A)).

- `project_env_keys(project)` — add — `set(read_env_file(<project>/".env"))`,
  `OSError`-guarded, returning an empty set when absent or unreadable.
- `sanitized_subprocess_env` — modify — strip `managed_secret_keys(...)` **and**
  `project_env_keys(...)`, then apply `own_env` (which still wins, so the
  declared credential is restored for the server that declared it).
- The docstring's "secrets the operator exported into the shell or a plain
  `.env` are not sanitized here" — modify — a plain `.env` now **is** stripped;
  shell-exported secrets still are not. Do not overclaim the second half.

### `src/pmcp/tools/handlers.py` (modify)

- `_check_api_key_available` — modify — answer from `env_store.read_env_file(path)`
  instead of `load_dotenv(path)` + `os.environ.get`. Check `os.environ` first
  (unchanged fast path), then each file's parsed dict. **No process state is
  mutated.** Docstring states that the check is now side-effect free and why.
- The `load_dotenv` import (`:21`) — **keep**, or update
  `tests/test_tools.py:3587` in the same change. **WAS WRONG (rev 1):** "delete"
  — that test monkeypatches `pmcp.tools.handlers.load_dotenv` and `setattr`
  raises when the attribute is gone. Deleting it is fine *if* the test is updated
  to patch the new seam; decide once, and state which.
- The `_manifest_server_to_config(..., os.environ.get)` call site — modify — pass
  the non-mutating resolver instead. **WAS WRONG (rev 1):** the resolver was
  specified in `loader.py` and never wired here, so decision (A) would not have
  worked.
- `read_env_file` calls — wrap in `try/except OSError` — an existing-but-unreadable
  `.env` must answer "no", not raise.

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

- **Removing** `cli.py`'s startup `load_dotenv()`. The gateway may keep reading
  `.env` for its own configuration; what changes is that those keys no longer
  reach downstream servers. **WAS WRONG (rev 1):** this was listed as simply out
  of scope, which is what let the primary leak path survive the fix.
- Sanitising shell-exported secrets. `sanitized_subprocess_env` inherits ambient
  environment by design; changing that is a separate decision with a much larger
  blast radius.
- The wider trust-boundary programme (#230). This fix stands alone and should not
  wait for it.

## Execution Policy

- execute: effort=medium, reason=small diff but it changes credential-resolution behaviour and the tests must prove absence rather than presence
