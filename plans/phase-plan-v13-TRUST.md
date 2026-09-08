---
phase_loop_plan_version: 1
phase: TRUST
roadmap: specs/phase-plans-v13.md
roadmap_sha256: b5f85f7b74a7d9f57bb57b3330ef912c04ac6304aee37a49f5a67345856116dc
---

# PHASE-1-TRUST: Trust primitives

## Context

TRUST is the freeze phase of the v13 trust-boundary roadmap. It publishes the two
contracts CONSENT and PKGID code against and **wires no caller** — that is
deliberate, and it is why the phase is small.

What exists today:

- `src/pmcp/env_store.py` already establishes the user-scope convention:
  `resolve_scope_path("user")` → `~/.config/pmcp/pmcp.env` (`:36-44`). The trust
  store belongs beside it, in the same user-scoped directory, which is what makes
  EC-TRUST-5 (store outside any repository) natural rather than novel.
- `read_env_file` (`:45-58`) shows the house style for reading a user-scoped file
  into a dict without touching global state — the #229 lesson, freshly applied.
- `src/pmcp/manifest/npm_resolver.py` provides `NpmResolver`, the frozen
  `NpmResolution` dataclass (`:195-215`), `get_resolver()` (`:619`) and
  `reset_resolver_for_tests()` (`:634`). Package identity **adapts** this; it does
  not start a new subsystem (roadmap Assumption 4).
- `src/pmcp/cli.py` registers subcommands with `subparsers.add_parser(...)`
  (`:291` onward) — `pmcp trust` follows that pattern.

Two constraints from the roadmap's rev-2 board that shape the design:

1. `is_approved` takes **the bytes the caller is about to apply**, not a path. A
   path-based check is defeated by swapping the file between check and use.
2. The store's **residency and write authority are part of the freeze**. A
   checkout-writable store lets a repository ship an approval for its own
   malicious content, which would make "absence is never assent" decorative.

## Interface Freeze Gates

- [ ] IF-0-TRUST-1 — `TrustRecord(absolute_path: Path, content_sha256: str, scope: str, decision: str, recorded_at: datetime)`; `is_approved(path: Path, content: bytes) -> bool`, `record(path: Path, content: bytes, scope: str, decision: str) -> TrustRecord`, `revoke(path: Path) -> bool`, `list_records() -> list[TrustRecord]`; store resides at `~/.config/pmcp/trust.json` (user scope) and refuses a path inside the current checkout. The freeze also fixes the semantics a downstream lane would otherwise guess: `decision` is the closed vocabulary `{"approved", "denied"}` and `is_approved` returns `True` only for `"approved"` — a `"denied"` record is not merely absent; the store keeps **one record per absolute path** (re-approving replaces, it does not append a history); every I/O or parse failure reading the store **fails closed** (`is_approved` returns `False`, never raises to a caller that might treat an exception as permission); and the store file is created mode `0o600` with the residency check resolving symlinks before comparing against the checkout root.
- [ ] IF-0-TRUST-2 — `PackageIdentity(registry: str, name: str, resolved_version: str, integrity: str | None)` and `resolve_package_identity(spec: str) -> PackageIdentity | None`, resolving without executing the package.

## Lane Index & Dependencies

SL-1 — Trust store core
  Depends on: (none)
  Blocks: SL-3, SL-4
  Parallel-safe: yes

SL-2 — Package identity adapter
  Depends on: (none)
  Blocks: SL-4
  Parallel-safe: yes

SL-3 — `pmcp trust` CLI verbs
  Depends on: SL-1
  Blocks: SL-4
  Parallel-safe: yes

SL-4 — Documentation & spec reconciliation
  Depends on: SL-1, SL-2, SL-3
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-1 — Trust store core

- **Scope**: The provenance/approval record, its user-scoped store, and the content-hash predicate every downstream loader will call.
- **Owned files**: `src/pmcp/trust_store.py`, `tests/test_trust_store.py`
- **Interfaces provided**: `TrustRecord`, `is_approved`, `record`, `revoke`, `list_records`, `trust_store_path`
- **Interfaces consumed**: (none)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-1.1 | test | — | `tests/test_trust_store.py` | **exactly these names**, because the acceptance criteria address them individually: `test_a_record_round_trips`, `test_changed_content_is_no_longer_approved`, `test_an_unknown_path_is_not_approved`, `test_an_unreadable_store_fails_closed`, `test_a_store_inside_the_checkout_is_refused`, `test_a_symlinked_store_into_the_checkout_is_refused`, `test_is_approved_judges_supplied_bytes_not_the_path`, `test_a_denied_record_is_not_approved` | `uv run pytest -q tests/test_trust_store.py` |
| SL-1.2 | impl | SL-1.1 | `src/pmcp/trust_store.py` | — | — |
| SL-1.3 | verify | SL-1.2 | `src/pmcp/trust_store.py`, `tests/test_trust_store.py` | all SL-1 tests | `uv run pytest -q tests/test_trust_store.py && uv run mypy src/` |

### SL-2 — Package identity adapter

- **Scope**: Resolve an npm spec to a stable identity tuple without executing it, adapting the existing #195 resolver.
- **Owned files**: `src/pmcp/manifest/package_identity.py`, `tests/test_package_identity.py`
- **Interfaces provided**: `PackageIdentity`, `resolve_package_identity`
- **Interfaces consumed**: (none — reads `npm_resolver` but does not modify it)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-2.1 | test | — | `tests/test_package_identity.py` | identity resolves offline from a fixture; an unresolvable spec returns `None` rather than raising; no subprocess is spawned | `uv run pytest -q tests/test_package_identity.py` |
| SL-2.2 | impl | SL-2.1 | `src/pmcp/manifest/package_identity.py` | — | — |
| SL-2.3 | verify | SL-2.2 | `src/pmcp/manifest/package_identity.py`, `tests/test_package_identity.py` | all SL-2 tests | `uv run pytest -q tests/test_package_identity.py && uv run mypy src/` |

### SL-3 — `pmcp trust` CLI verbs

- **Scope**: `pmcp trust approve <path>`, `pmcp trust list`, `pmcp trust revoke <path>` over the SL-1 store.
- **Owned files**: `src/pmcp/cli.py`, `tests/test_trust_cli.py`
- **Interfaces provided**: the `pmcp trust` subcommand surface
- **Interfaces consumed**: `TrustRecord`, `is_approved`, `record`, `revoke`, `list_records` (SL-1)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-3.1 | test | — | `tests/test_trust_cli.py` | each verb round-trips through the store; `approve` on a missing file exits non-zero; `list` output names path and decision | `uv run pytest -q tests/test_trust_cli.py` |
| SL-3.2 | impl | SL-3.1 | `src/pmcp/cli.py` | — | — |
| SL-3.3 | verify | SL-3.2 | `src/pmcp/cli.py`, `tests/test_trust_cli.py` | all SL-3 tests | `uv run pytest -q tests/test_trust_cli.py tests/test_cli.py` |

### SL-docs — Documentation & spec reconciliation

- **Scope**: Refresh the docs catalog, update cross-cutting documentation touched or invalidated by this phase's impl lanes, and append any post-execution amendments to phase specs whose interface freezes turned out wrong.
- **Owned files**: `.claude/docs-catalog.json`, `CHANGELOG.md`, `specs/phase-plans-v13.md`
- **Interfaces provided**: (none)
- **Interfaces consumed**: (none)
- **Parallel-safe**: no (terminal)
- **Depends on**: SL-1, SL-2, SL-3
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Action |
|---|---|---|---|---|
| SL-docs.1 | docs | — | `.claude/docs-catalog.json` | Rescan via `_shared/scaffold_docs_catalog.py --rescan` if present; if absent, record "docs-catalog rescan helper unavailable; manual catalog audit" in the commit message and proceed. |
| SL-docs.2 | docs | SL-docs.1 | per catalog | Decide per catalog file whether this phase changes it. **SECURITY.md is deliberately NOT updated here** — the roadmap assigns the trust-model write-up to SEAL, once, rather than four partial descriptions. Record that skip explicitly. |
| SL-docs.3 | docs | SL-docs.2 | `specs/phase-plans-v13.md` | Append `### Post-execution amendments` to the TRUST phase section if either IF-0-TRUST gate proved wrong in practice. |
| SL-docs.4 | verify | SL-docs.3 | — | `uv run ruff format --check src/ tests/ scripts/` (same paths as `## Verification`) plus any repo doc linters; no-op if none configured. |

## Execution Notes

- **Machine validators unavailable in this checkout.** `scripts/validate_plan_doc.py`
  does not exist in this repo and `phase_loop_runtime.planner_validation` is not
  importable from this worktree's interpreter. The Lane validation checklist was
  walked by hand: file ownership is disjoint (`trust_store.py` / `package_identity.py`
  / `cli.py`), the DAG is acyclic, every `impl` follows a `test`, and every
  acceptance item names a proving command. `## Dispatch Hints` is deliberately
  omitted rather than authored unvalidated. The skill's advisor-review step
  (7.75) was also unavailable in this session; this plan has had **no** automated
  review, only the hand-walked checklist and the roadmap's own board round — treat
  its freezes as slightly less hardened than the roadmap's. **A board round was
  subsequently run** and produced three real findings, all folded in: the freeze
  left `decision` semantics, record cardinality, fail-closed I/O and write
  authority unstated; the acceptance criteria used `-k` expressions guessing at
  test names that do not exist yet (measured: a non-matching `-k` exits 5, so it
  fails loudly rather than passing vacuously — but it proves nothing either, and
  an implementer would have had to reverse-engineer the intended names); and the
  stale-base guidance still carried the template's unfilled placeholder. Two of
  three seats spent their verdict objecting that a plan contains no
  implementation evidence, which is what a plan is — those objections are not
  folded in.
- **Single-writer files**: `src/pmcp/cli.py` — owner **SL-3**. No other lane in this
  phase touches it. **Correction (2026-09-08):** an earlier revision of this note
  claimed "CONSENT and PKGID do not write `cli.py`". PKGID's plan does write it —
  the cross-cutting principle requires every refusal to name a runnable `pmcp`
  command, which means an approval verb. No concurrent-writer edge results (TRUST
  is merged before PKGID opens, and CONSENT touches no `cli.py`), but the claim
  as written was false and would have mis-set a later planner's expectations.
- **Known destructive changes**: none — every lane is purely additive. `SL-3` adds a
  subparser to `cli.py` and modifies no existing verb.
- **Expected add/add conflicts**: none — there is no SL-0 preamble lane; SL-1 and SL-2
  create new modules that no other lane stubs.
- **SL-0 re-exports**: not applicable — this phase adds no package `__init__.py`
  re-exports. If a later phase wants `pmcp.trust_store` re-exported from a package
  `__init__`, use the `__getattr__` lazy form.
- **Parallelism**: SL-1 and SL-2 are DAG roots and share no files — run them
  concurrently. SL-3 opens when SL-1's interfaces land; it consumes them and must not
  begin against a guessed signature.
- **Do not wire any caller.** No loader, policy path, or provisioning path changes in
  this phase. A lane that "helpfully" wires CONSENT's or PKGID's consumer has broken
  the freeze and made those phases un-parallel.
- **Stale-base guidance** (copy verbatim): Lane teammates working in isolated worktrees do not see sibling-lane merges automatically. If a lane finds its worktree base is pre-SL-1 (for SL-3, the only lane here with an upstream), it MUST stop and report rather than committing — the orchestrator will re-spawn or rebase. Silent `git reset --hard` or `git checkout HEAD~N -- …` in a stale worktree produces commits that destroy peer-lane work on `--no-ff` merge.

## Acceptance Criteria

- [ ] EC-TRUST-1 — proven by `uv run pytest -q tests/test_package_identity.py`, falsified by a negative control that resolves a spec with the network and any subprocess spawn monkeypatched to raise (identity must still resolve from the fixture, and no spawn is attempted).
- [ ] EC-TRUST-2 — proven by `uv run pytest -q tests/test_trust_store.py::test_a_record_round_trips tests/test_trust_store.py::test_changed_content_is_no_longer_approved`, falsified by approving a file, mutating one byte, and asserting `is_approved` returns `False`.
- [ ] EC-TRUST-3 — proven by `uv run pytest -q tests/test_trust_store.py::test_an_unknown_path_is_not_approved tests/test_trust_store.py::test_an_unreadable_store_fails_closed`, falsified by querying a path with no record and asserting `False` (never a default-true or a raise swallowed to true).
- [ ] EC-TRUST-4 — proven by `uv run pytest -q tests/test_trust_cli.py`, falsified by invoking each of `approve` / `list` / `revoke` and asserting the store state changed as named; `approve` must exist, because every downstream refusal message names it.
- [ ] EC-TRUST-5 — proven by `uv run pytest -q tests/test_trust_store.py::test_a_store_inside_the_checkout_is_refused tests/test_trust_store.py::test_a_symlinked_store_into_the_checkout_is_refused`, falsified by pointing the store at a path inside the checkout — directly and via symlink — and asserting refusal rather than a read.
- [ ] EC-TRUST-6 — proven by `uv run pytest -q tests/test_trust_store.py::test_is_approved_judges_supplied_bytes_not_the_path`, falsified by approving content A, then calling `is_approved(path, content_B)` for a file whose on-disk bytes are still A, and asserting `False` — the predicate must judge the supplied bytes, not the path.

## Verification

```bash
uv run pytest -q tests/test_trust_store.py tests/test_package_identity.py tests/test_trust_cli.py
uv run pytest -q tests/                       # compare counts to the pre-phase baseline, same dir
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
uv run python -c "import pmcp.trust_store, pmcp.manifest.package_identity"   # both importable
git diff --name-only origin/main..HEAD -- src/pmcp/policy src/pmcp/config src/pmcp/manifest/loader.py
# ^ MUST be empty: TRUST wires no caller. A non-empty result means the freeze leaked.
```

Host note: `/tmp/package.json` makes ~107 npm-identity tests fail on some hosts
(`tests/conftest.py`); compare the same command from the same directory before and
after, never against a clean-machine expectation.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `src/pmcp/trust_store.py`, `src/pmcp/manifest/package_identity.py`, `src/pmcp/cli.py`
- evidence paths: `tests/test_trust_store.py`, `tests/test_package_identity.py`, `tests/test_trust_cli.py`, `CHANGELOG.md`
- redaction posture: `metadata_only`
- downstream handling: none — SECURITY.md's trust-model write-up is owned by SEAL

## Execution Policy

- default: effort=low, reason=two new self-contained modules plus one subparser
- SL-1: effort=high, reason=security predicate where a wrong default silently grants trust
- SL-2: effort=medium, reason=adapts an existing resolver whose refusal semantics are subtle
