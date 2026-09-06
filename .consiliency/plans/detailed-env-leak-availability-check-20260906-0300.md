# Detailed plan: stop the project `.env` reaching downstream servers

> **Revision 3 (2026-09-06).** Revs 1 and 2.1 boarded DISAGREE. Six defects
> across them, all one class: **both revisions tried to re-derive what
> `load_dotenv` had done — which keys it introduced, how it interpolated, how
> empty values shadow — instead of simply recording it.** Rev 3 changes the
> mechanism rather than patching the findings:
>
> - **rev 1** fixed `_check_api_key_available` and declared `cli.py` out of
>   scope, leaving the primary leak path (`cli.py:2699`, the entry point) open.
> - **rev 2.1** moved the fix to the subprocess boundary but stripped **by key
>   name**, which would delete a shell-provided `PATH` that merely shares a name
>   with a `.env` entry; and its end-to-end test could pass **without the
>   sanitiser being touched at all**, because once the availability check stops
>   mutating, the sentinel never enters `os.environ` for the test to find.
> - Both left `interpolate=False` vs `load_dotenv` and empty-value shadowing
>   unresolved, and rev 2.1's "Dependencies & order" never listed its own
>   load-bearing `env_store` step.
>
> **Rev 3 records provenance instead of reconstructing it.** No dotenv file is
> re-parsed, no semantics are emulated, and `load_dotenv` keeps its exact current
> behaviour everywhere it is called.

## Task

Close Consiliency/pmcp#229 (review finding **S-02**, HIGH): keys from the
operator's project `.env` are inherited by every downstream MCP server PMCP
spawns.

## Research summary

Verified against `origin/main` at `aa2de50`.

**Two sites put `.env` into `os.environ`, not one.**

1. `src/pmcp/cli.py:2699` — bare `load_dotenv()` in `main()`, which
   `pyproject.toml:134` declares as the console-script entry
   (`pmcp = "pmcp.cli:main"`). It loads `<cwd>/.env` unconditionally at startup,
   before anything else. **This is the primary path**, and rev 1 wrongly called
   it out of scope.
2. `src/pmcp/tools/handlers.py:2942` — `load_dotenv(env_path)` inside
   `_check_api_key_available`, which answers a **boolean question** by mutating
   global process state, loading every key in the file rather than the one asked
   about.

**The inheritance is real and the code documents it.**
`env_store.sanitized_subprocess_env` (`src/pmcp/env_store.py:124`) builds each
server's environment from `os.environ.copy()` minus PMCP-managed keys, and its
own docstring records the gap: *"this removes only PMCP-managed keys; secrets the
operator exported into the shell **or a plain `.env`** are not sanitized here."*

**It is the single choke point.** Every downstream environment goes through it:
`client/manager.py:2343` (whose result is the only `env=` at `:2354`),
`manifest/installer.py:542`, and `tools/handlers.py:5143` / `:5247`.

**Why "strip by name" is wrong (rev 2.1's defect).** `load_dotenv` defaults to
`override=False`, so a variable already in the environment is **not** replaced. A
shell-exported `PATH` that also appears in `.env` was never sourced from that
file — stripping it by name would remove the operator's `PATH` from every
spawned server.

**Why re-parsing is wrong.** `read_env_file` uses `dotenv_values(...,
interpolate=False)`. Measured difference: for `DERIVED=${BASE}/x` it returns the
literal `${BASE}/x` where `load_dotenv` yields `abc/x`. Empty values differ too:
under `load_dotenv`'s no-override semantics an empty higher-priority value blocks
a later file, which per-file dicts do not reproduce. Any fix that re-parses must
emulate both, and emulation is exactly what keeps going wrong.

## Design: record what was introduced, strip that

One mechanism, applied wherever PMCP loads a dotenv file into its own process:

```python
before = set(os.environ)
load_dotenv(...)                      # unchanged semantics
introduced = set(os.environ) - before # exactly the keys THIS load added
```

`introduced` is precise by construction: a shell-provided `PATH` is in `before`,
so it is never recorded and never stripped. Interpolation and empty-value
shadowing need no emulation — whatever `load_dotenv` did is what gets recorded.

`sanitized_subprocess_env` then strips the recorded keys in addition to
`managed_secret_keys`, and `own_env` is applied **after** the strip, so a
server's own declared credential is restored (operator decision **(A)**: a plain
`.env` may still supply a server's own `env_var` — that key only, that server
only).

## Changes

### `src/pmcp/env_store.py` (modify)

- `record_dotenv_keys(keys: Iterable[str]) -> None` and
  `dotenv_sourced_keys() -> frozenset[str]` — add — a module-level registry of
  keys PMCP itself introduced from dotenv files. Additive and idempotent.
  Document that it holds *provenance*, not file contents, and that an empty
  registry (library use, `main()` never ran) is the correct default.
- `reset_dotenv_keys()` — add — test-only seam, so the registry cannot leak
  between tests. Name it so its purpose is unmistakable.
- `sanitized_subprocess_env` — modify — strip `managed_secret_keys(...)` **and**
  `dotenv_sourced_keys()`, then apply `own_env` (unchanged precedence, so the
  declared credential still wins). Signature and existing callers unchanged.
- The docstring's "or a plain `.env`" gap sentence — modify — a plain `.env` is
  now stripped; shell-exported secrets still are not. Do not overclaim.

### `src/pmcp/cli.py` (modify)

- `load_startup_env()` — add — a **named, importable** function holding the three
  startup loads currently inline in `main()` (`:2696-2702`), with the
  before/after delta captured around the **first** `load_dotenv()` (the plain
  `.env`) and passed to `record_dotenv_keys`. The two PMCP-store loads need no
  recording: `managed_secret_keys` already covers them. `load_dotenv` itself is
  unchanged — the gateway still reads `.env` for its own configuration.
- `main()` — modify — call `load_startup_env()` in place of the inline loads.

  **Why extract rather than inline the delta:** recording lives at the call site,
  not inside `load_dotenv`. If the end-to-end test calls `load_dotenv` itself,
  nothing is recorded and the test fails even against a correct implementation;
  if the test calls `record_dotenv_keys` itself, it passes even when `main()`
  never records — leaving the primary leak path unproven. A named function is
  production code the test can execute directly, so the proof exercises the real
  startup sequence.

### `src/pmcp/tools/handlers.py` (modify)

- `_check_api_key_available` (`:2926`) — modify — keep `load_dotenv` and its
  exact semantics, but wrap each call in the same before/after delta and record
  it. **The boolean answer is bit-for-bit unchanged**, which retires the
  interpolation and empty-value-shadowing findings entirely: nothing is
  re-parsed and no precedence rule is re-implemented. The leak closes because the
  keys it introduces are now recorded and stripped at the subprocess boundary.
- The `load_dotenv` import (`:21`) — **keep**. `tests/test_tools.py:3587`
  monkeypatches `pmcp.tools.handlers.load_dotenv`; that test keeps working
  unchanged.

### `tests/test_env_leak_229.py` (create)

Each must fail on unchanged `main`:

- `test_a_startup_env_secret_never_reaches_a_spawned_server` — **the honest
  end-to-end proof.** Call the **production** `cli.load_startup_env()` against a
  `.env` holding a sentinel PMCP does not manage, then assert the sentinel **is**
  in `os.environ` — the gateway may see it — and **is not** in
  `sanitized_subprocess_env()`.
  **WAS WRONG (rev 2.1):** asserted absence after the availability check, which
  passes once that check stops mutating even if the sanitiser is untouched.
  **WAS WRONG (rev 3):** called `load_dotenv` directly. Recording lives at the
  call site, so that test either fails against a correct fix (nothing recorded)
  or, if patched to call `record_dotenv_keys` itself, passes while `main()` still
  leaks. Neither pins the primary path. The test must run production code.
- `test_main_uses_the_recording_startup_path` — the wiring guard: `main()` must
  call `load_startup_env()`, asserted by patching it and requiring it to be
  invoked (or by AST, as in the #225 guards). Without this, the extraction can be
  bypassed by a future edit re-inlining a bare `load_dotenv()` and every other
  test still passes.
- `test_a_shell_provided_variable_sharing_a_name_is_not_stripped` — put a
  variable already present in the environment into the `.env` too; it must
  survive into the child env with the operator's value. This is rev 2.1's
  `PATH`-deletion defect, pinned.
- `test_the_availability_check_records_what_it_loads` — after
  `_check_api_key_available`, a sentinel it loaded is absent from
  `sanitized_subprocess_env()`.
- `test_the_availability_boolean_is_unchanged` — the same {present, absent} ×
  {`os.environ`, `.env`, `.env.pmcp`, `pmcp.env`} matrix answers identically to
  `main`, **including** an interpolated value and an empty value in a
  higher-priority file. Feasible now precisely because the semantics are untouched.
- `test_the_servers_own_credential_still_resolves` and
  `test_only_the_declared_key_is_injected` — decision (A), both halves.
- `test_the_registry_is_empty_without_main` — importing the library and never
  running `main()` records nothing and strips nothing.

## Documentation impact

- `SECURITY.md` — modify — the credential-isolation bullet and the
  `sanitized_subprocess_env` gap it mirrors. State precisely what is now true: a
  plain `.env` is stripped; shell-exported secrets are still inherited.
- `CHANGELOG.md` — add — `### Security`. Required: this changes `src/`.

## Dependencies & order

1. `env_store` registry + `sanitized_subprocess_env` strip — **the load-bearing
   change**. **WAS WRONG (rev 2.1):** its order list omitted this step entirely
   while naming it the load-bearing one in Changes.
2. `tests/test_env_leak_229.py`, red against `main`.
3. `cli.py` startup recording.
4. `handlers.py` availability-check recording.
5. Docs.

## Verification

```bash
uv run pytest -q tests/test_env_leak_229.py
uv run pytest -q tests/test_tools.py                  # the monkeypatch at :3587 still works
uv run pytest -q tests/                               # compare to main, same dir
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
grep -rn "load_dotenv" src/                           # cli.py + handlers.py, both now recorded
```

Host note: `/tmp/package.json` makes ~107 npm-identity tests fail here
(`tests/conftest.py`); compare the same command from the same directory before
and after.

Edge cases: `.env` absent or unreadable (record nothing, never raise); the same
key in `.env` and a PMCP store (`managed_secret_keys` already strips it;
double-strip is harmless); a server whose declared `env_var` is also a managed
key (`own_env` wins, unchanged); registry state across tests (use the reset seam).

## Acceptance criteria

- [ ] With `os.environ` populated as `main()` leaves it, a `.env` sentinel is
      **present in `os.environ`** and **absent from `sanitized_subprocess_env()`**.
      On unchanged `main` it is present in both.
- [ ] A shell-provided variable that also appears in `.env` is **not** stripped —
      the child env still has the operator's value.
- [ ] The availability boolean is identical to `main` across the full
      {present, absent} × four-source matrix, including an interpolated value and
      an empty value shadowing a later file.
- [ ] A server whose declared `env_var` lives only in `.env` still receives it,
      and a second key in that same file does not.
- [ ] `tests/test_tools.py` passes unmodified (the `load_dotenv` monkeypatch).
- [ ] Full-suite failures/skips/deselects unchanged from `main`; passes increase
      by exactly the new tests.

## Non-goals

- **Removing** `cli.py`'s startup `load_dotenv()`. The gateway may keep reading
  `.env` for its own configuration; what changes is that those keys stop being
  inherited by downstream servers.
- Sanitising shell-exported secrets — deliberate, and out of scope.
- The wider trust-boundary programme (#230).

## Execution Policy

- execute: effort=medium, reason=small diff but it changes credential-resolution behaviour and the proofs must assert absence at the subprocess boundary rather than in the gateway
