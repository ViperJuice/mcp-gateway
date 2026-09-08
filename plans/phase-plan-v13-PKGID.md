---
phase_loop_plan_version: 1
phase: PKGID
roadmap: specs/phase-plans-v13.md
roadmap_sha256: cdc52f20a48d07f679a7a86d44c0eeb5ab6ed69e7e7676980b5e0379df2ca7eb
---

# PHASE-3-PKGID: Provisioning binds to package identity

## Context

PKGID is the roadmap's critical path and closes S-01, the review's one HIGH finding.
Every line below was re-verified against this worktree's `HEAD` (`ada17ae`), because
the review's line numbers have drifted since it was written.

**The hole, as it exists today.**
`register_discovered_server` (`src/pmcp/tools/handlers.py:5513-5587`; review said
`:5496-5571`) accepts any `package` that clears `is_valid_package_name`
(`src/pmcp/validation.py:16-30`) and stores
`ServerConfig(command="npx", args=["-y", package], install={<every platform>: ["npx","-y",package]})`

  **Both argv are pinned, not just `install`.** `client/manager.py:2349` spawns the
  server with `*local_config.args`, so pinning only `install_command` would approve
  `pkg@1.2.3`, install those bytes, and then re-resolve `npx -y pkg` to *latest* at
  every subsequent server start — defeating this phase at the spawn path. Registration
  writes the resolved spec into `args` as well: `args == ["-y", f"{name}@{version}"]`.
into `self._discovered_server_configs` (`:5550`). `provision` (`:4127`) then checks
`self._policy_manager.is_server_allowed(server_name)` (`:4135`) — the **agent-supplied
name** — and reaches `job_manager.start_install(server_config, platform)` (`:4483`),
which is `asyncio.create_subprocess_exec(*install_cmd, …)`
(`src/pmcp/manifest/installer.py:135`). Nothing ties the name to the package and
nothing asks a human.

**Roadmap Assumption 3 — verified, holds, with line drift.** `provision` still
consults the shipped manifest **before** the discovered registry:
`manifest = load_manifest()` (`handlers.py:4279`), `manifest.get_server(server_name)`
(`:4280`), and only then `if not server_config: server_config =
self._discovered_server_configs.get(server_name)` (`:4282-4283`). The review cited
`:4262-4266`; the ordering is intact, the range moved +17 lines. S-01's blast radius
is as stated, not larger.

**Three facts that shape the design, each grepped rather than assumed.**

1. **Nothing is persisted to migrate.** `_discovered_server_configs` is a plain
   instance dict (`handlers.py:1126`); `server.py:387-388` only dispatches the tool
   call. No file, no state store, no restore path. EC-PKGID-6's migration surface is
   therefore *empty of durable records* — see the criterion for what that means.
2. **`start_install` has exactly one production callsite** (`handlers.py:4483`; all
   other hits are `tests/`). A gate placed there covers every install spawn reachable
   from the agent.
3. **Three spawn sites exist in `installer.py`**: `:135` (`start_install`, live),
   `:609` (`install_server`, legacy — **no production callers**), `:651`
   (`verify_installation`, spawns the agent-supplied `server_config.command`).
   EC-PKGID-4 says *every* install spawn, so SL-3 logs all three rather than only the
   live one.

**Two constraints inherited from TRUST (merged).** `src/pmcp/trust_store.py` publishes
`trust_store_path`, `is_approved(path, content)`, `record`, `revoke`, `list_records`;
`src/pmcp/manifest/package_identity.py` publishes
`PackageIdentity(registry, name, resolved_version, integrity)` and
`resolve_package_identity(spec) -> PackageIdentity | None`. PKGID consumes both and
extends neither's file.

## Interface Freeze Gates

- [ ] IF-0-PKGID-1 — the provisioning gate, `src/pmcp/provision_gate.py`:
  `ProvisionSource = Literal["manifest", "configured", "discovered"]`;
  `ProvisionDecision(allowed: bool, reason: str, remedy: str | None, identity: PackageIdentity | None)` (frozen dataclass);
  `evaluate_provision(server_config: ServerConfig, identity: PackageIdentity | None, *, source: ProvisionSource, policy: PolicyManager) -> ProvisionDecision`.
  The freeze fixes the **decision order**, which a downstream lane would otherwise
  guess, and every step fails closed: (1) `evaluate_package_policy(identity) ==
  "denied"` denies regardless of source — deny always wins; (2) `source == "manifest"`
  allows, reason `manifest_backed`; (3) a non-manifest source with `identity is None`
  denies, reason `unresolvable_identity`; (4) a recorded package approval for
  `(registry, name, resolved_version)` allows, reason `package_approved`; (5)
  `evaluate_package_policy(identity) == "allowed"` allows, reason `policy_allowed`;
  (6) otherwise — including `"unspecified"` — deny,
  reason `not_approved`; (7) **any** exception reading the store or the policy denies
  rather than propagating — a caller must never be able to read a raise as
  permission. `reason` is that closed six-value vocabulary. `remedy` is `None` when allowed, and otherwise
  **depends on the reason** — a single frozen string is unconstructible for two of the
  deny branches. `package_approved`-shaped refusals (`not_approved`) use the exact
  runnable `pmcp trust approve-package <name>@<resolved_version>`. Reason
  `unresolvable_identity` has no `identity`, so there is no name or version to compose:
  its remedy names the registry lookup that failed and is not an approval command.
  Reason `denied` (policy denylist) takes precedence over any approval by rule 1, so
  offering `approve-package` there would advertise a remedy that cannot work; its remedy
  points at the operator's policy file instead. `source` is decided by the
  **lookup path that produced the config**, never by a field on `server_config` — the
  agent controls those fields.
  The policy predicate is deliberately **tri-state, not a bool**. A bool cannot carry
  this order: rule 1 denies before the manifest exemption and rule 5 allows after it,
  so a manifest server with a denylisted package and a manifest server in no list
  would both return `False` while requiring opposite outcomes. Worse, a bool
  implemented in the house style of `is_server_allowed` (`policy.py:161-175`) returns
  `True` when no section is configured, which rule 5 would read as an allowlist match
  — every discovered package allowed with no approval, two faithful lanes composing
  back into the exact S-01 hole this phase closes. `"unspecified"` falls through to
  rule 6 and denies.
- [ ] IF-0-PKGID-2 — *(plan-local extension of IF-0-PKGID-1; not a roadmap-declared gate)* the package-approval store, `src/pmcp/package_approvals.py`:
  `PackageApproval(registry: str, name: str, resolved_version: str, integrity: str | None, decision: str, recorded_at: datetime)`;
  `approve_package(identity) -> PackageApproval`, `is_package_approved(identity) -> bool`,
  `revoke_package(name: str, version: str | None = None) -> bool`, `list_package_approvals() -> list[PackageApproval]`.
  Residency is **consumed, not re-litigated**: the file is
  `trust_store_path().parent / "package_approvals.json"`, so TRUST's user-scope and
  outside-the-checkout rules apply unchanged. Mode `0o600`. `decision` is the closed
  vocabulary `{"approved", "denied"}`; one record per
  `(registry, name, resolved_version)`, so **a version bump is a new record needing a
  new approval** — that is the entire point of the pin. Every I/O or parse failure
  returns `False` from `is_package_approved` and never raises.
  **This phase owns its own test isolation and must not inherit CONSENT's.** CONSENT
  SL-1.3 owns the autouse `tests/conftest.py` redirect of `trust_store_path()`, but this
  phase does not own `conftest.py`, and the roadmap requires only that CONSENT and PKGID
  execute *serially* — not that CONSENT goes first. Executed first, PKGID's
  `approve_package` tests would write **real approvals into the operator's
  `~/.config/pmcp/package_approvals.json`**, and those records then satisfy
  `evaluate_provision` rule 4 and permit a real provision. SL-1 therefore declares an
  autouse fixture inside its own owned test files (`tests/test_package_approvals.py`,
  `tests/test_package_identity_gate.py`) that redirects the store to `tmp_path`,
  independent of execution order and of whether CONSENT has landed.

## Lane Index & Dependencies

SL-1 — Provisioning gate, registration pinning, and operator surface
  Depends on: SL-2
  Blocks: SL-4
  Parallel-safe: yes

SL-2 — Policy package identifiers and spec parsing
  Depends on: (none)
  Blocks: SL-1, SL-4
  Parallel-safe: yes

SL-3 — Install-spawn argv logging
  Depends on: (none)
  Blocks: SL-4
  Parallel-safe: yes

SL-4 — Documentation & spec reconciliation
  Depends on: SL-1, SL-2, SL-3
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-1 — Provisioning gate, registration pinning, and operator surface

- **Scope**: The single call `provision` consults before an install may spawn, the version pin recorded at registration, the approval store behind it, and the `pmcp trust` package verbs every refusal message names.
- **Owned files**: `src/pmcp/provision_gate.py`, `src/pmcp/package_approvals.py`, `src/pmcp/tools/handlers.py`, `src/pmcp/cli.py`, `tests/test_package_identity_gate.py`, `tests/test_package_approvals.py`
- **Interfaces provided**: `ProvisionDecision`, `ProvisionSource`, `evaluate_provision`, `PackageApproval`, `approve_package`, `is_package_approved`, `revoke_package`, `list_package_approvals`, the `pmcp trust approve-package|list-packages|revoke-package` verbs
- **Interfaces consumed**: `PackageIdentity`, `resolve_package_identity` (TRUST SL-2); `trust_store_path` (TRUST SL-1); `evaluate_package_policy`, `PackagePolicy` (SL-2); `parse_package_spec`, `is_valid_package_version` (SL-2, `validation.py`)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-1.1 | test | — | `tests/test_package_identity_gate.py`, `tests/test_package_approvals.py` | **exactly these names**, because the acceptance criteria address them individually: `test_the_review_reproduction_does_not_spawn_npx_for_an_arbitrary_package`, `test_the_refusal_names_the_package_and_the_approval_command`, `test_discovered_provisioning_is_denied_without_opt_in`, `test_a_manifest_backed_server_provisions_unchanged`, `test_a_resolvable_registration_records_the_version_and_pins_the_install_argv`, `test_an_unresolvable_registration_is_refused_at_registration`, `test_a_resolved_version_that_fails_validation_is_refused`, `test_an_approved_package_provisions_after_the_refusal`, `test_a_store_read_failure_denies_rather_than_raises`, `test_source_is_taken_from_the_lookup_path_not_from_server_config`, `test_policy_denylist_blocks_an_approved_package`, `test_policy_package_allowlist_permits_provision_without_a_recorded_approval`, `test_package_denylist_beats_a_recorded_approval` | `uv run pytest -q tests/test_package_identity_gate.py tests/test_package_approvals.py` |
| SL-1.2 | impl | SL-1.1 | `src/pmcp/provision_gate.py`, `src/pmcp/package_approvals.py` | — | — |
| SL-1.3 | impl | SL-1.1 | `src/pmcp/tools/handlers.py` | — | — |
| SL-1.4 | impl | SL-1.1 | `src/pmcp/cli.py` | — | — |
| SL-1.5 | verify | SL-1.4 | `src/pmcp/provision_gate.py`, `src/pmcp/package_approvals.py`, `src/pmcp/tools/handlers.py`, `src/pmcp/cli.py` | all SL-1 tests | `uv run pytest -q tests/test_package_identity_gate.py tests/test_package_approvals.py tests/test_tools.py tests/test_provision_validation.py tests/test_trust_cli.py && uv run mypy src/` |

`handlers.py` edits are deliberately thin: `register_discovered_server` resolves and
validates the version then pins `["npx","-y",f"{name}@{version}"]` into **both**
`install` and `args`. **`source == "configured"` is pinned too, and this is not
optional.** A `.mcp.json` server reaches `evaluate_provision` as `"configured"` and can
be allowed by rule 4 (a recorded approval) or rule 5 (policy allowlist) — but nothing in
the registration path rewrote its argv, so it can be approved at resolved version X and
then spawn `npx -y pkg`, which resolves to Y. That is the same check-then-use gap the
runtime-argv pin closes for discovered servers, reached by a different door. A configured
config whose argv is not version-pinned is therefore **refused** with reason
`unresolvable_identity` rather than approved against an identity its argv does not name;
the operator pins the version in `.mcp.json` (or re-registers) to proceed.
`provision` calls `evaluate_provision` immediately before `start_install`
(`:4483`) with `source` set from which lookup produced the config
(`:4280` → `"manifest"`, `:4283` → `"discovered"`, the `.mcp.json` branch →
`"configured"`). Refusals ride the **existing** `message` field of `ProvisionOutput`
and `RegisterDiscoveredServerOutput`, and the pinned argv rides the existing
`install_command` field — SL-1 adds no field to `types.py`, which SL-2 owns.

### SL-2 — Policy package identifiers and spec parsing

- **Scope**: Let policy allow and deny package identifiers alongside server names, and publish the spec/version parsing both lanes need.
- **Owned files**: `src/pmcp/policy/policy.py`, `src/pmcp/types.py`, `src/pmcp/validation.py`, `tests/test_policy_package_identifiers.py`
- **Interfaces provided**: `PackagePolicy` (`allowlist`/`denylist`, glob-matched like the existing sections), `GatewayPolicy.packages`, `PolicyManager.evaluate_package_policy(identity) -> Literal["denied", "allowed", "unspecified"]`, `parse_package_spec(spec) -> tuple[str, str | None]`, `is_valid_package_version(version) -> bool`
- **Interfaces consumed**: `PackageIdentity` (TRUST SL-2)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-2.1 | test | — | `tests/test_policy_package_identifiers.py` | **exactly these names**: `test_a_policy_with_no_packages_section_returns_unspecified`, `test_evaluate_package_policy_denylist_beats_allowlist`, `test_evaluate_package_policy_matches_a_scoped_name_glob`, `test_a_registry_supplied_version_with_metacharacters_is_rejected`, `test_parse_package_spec_splits_a_scoped_name_from_its_version` | `uv run pytest -q tests/test_policy_package_identifiers.py` |
| SL-2.2 | impl | SL-2.1 | `src/pmcp/types.py`, `src/pmcp/validation.py` | — | — |
| SL-2.3 | impl | SL-2.2 | `src/pmcp/policy/policy.py` | — | — |
| SL-2.4 | verify | SL-2.3 | `src/pmcp/policy/policy.py`, `src/pmcp/types.py`, `src/pmcp/validation.py` | all SL-2 tests | `uv run pytest -q tests/test_policy_package_identifiers.py tests/test_policy.py tests/test_validation.py && uv run mypy src/` |

`is_valid_package_name` (`validation.py:16-30`) rejects `@` outside a leading scope,
so it rejects the composed spec `pkg@1.2.3`. Name and version are therefore validated
**separately** and composed afterwards. The resolved version arrives from a registry
response — semi-trusted network data bound for argv — so `is_valid_package_version`
is a strict allowlist pattern, not a metacharacter denylist.

SL-2's tests are **pure policy-layer tests** — tri-state matching, version validation,
spec parsing. The gate-composition assertions that EC-PKGID-2 needs (denylist beating
a recorded approval, allowlist standing in for one) live in SL-1's file, because SL-2
runs first and cannot import a gate that does not exist yet; putting them here would
fail SL-2.4 at collection.

### SL-3 — Install-spawn argv logging

- **Scope**: Log the exact argv at WARNING before every subprocess spawn in the installer, including the paths that subsequently fail.
- **Owned files**: `src/pmcp/manifest/installer.py`, `tests/test_install_argv_logging.py`
- **Interfaces provided**: (none — behaviour change only)
- **Interfaces consumed**: (none)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-3.1 | test | — | `tests/test_install_argv_logging.py` | **exactly these names**: `test_start_install_logs_rendered_argv_at_warning`, `test_legacy_install_server_logs_rendered_argv_at_warning`, `test_verify_installation_logs_rendered_argv_at_warning`, `test_argv_is_logged_even_when_the_spawn_fails` | `uv run pytest -q tests/test_install_argv_logging.py` |
| SL-3.2 | impl | SL-3.1 | `src/pmcp/manifest/installer.py` | — | — |
| SL-3.3 | verify | SL-3.2 | `src/pmcp/manifest/installer.py`, `tests/test_install_argv_logging.py` | all SL-3 tests | `uv run pytest -q tests/test_install_argv_logging.py tests/test_manifest.py tests/test_manifest_provision.py` |

Today `start_install` logs at INFO with the literal `<args redacted>`
(`installer.py:126-128`). The blanket redaction is what makes an arbitrary package
execution invisible in the operator's log, but the premise that argv never carries a
secret is **false as stated**: the shipped manifest's install commands are not all
`npx` (167 `npx`, 38 `uvx`, 1 `cmd`), and a manifest may supply an arbitrary install
command whose arguments could carry a credential. So the log is not un-redacted
wholesale. The contract is therefore **secret-safe by construction, not by
executable allowlist** — an earlier revision logged `npx`/`uvx` verbatim and kept
redaction elsewhere, which both left credential-bearing `npx … --token=…` argv exposed
and contradicted EC-PKGID-4's requirement that every spawn log its argv. Every spawn logs
a **rendered** argv: the executable verbatim, plus the validated `PackageIdentity`
(`name@resolved_version`) when one is present, plus each remaining argument either
verbatim when it matches the frozen safe shape (a flag, or a package spec that passed
`is_valid_package_name`/`is_valid_package_version`) or replaced by `<redacted>`. The
package identity — the thing this phase exists to make visible — is always shown; an
argument that could carry a credential never is. Secrets otherwise
live in the child env built by `build_install_child_env`, not in argv. Each site logs immediately before
its `create_subprocess_exec` (`:135`, `:609`, `:651`), inside the `try` but ahead of
the call, so a `FileNotFoundError` spawn still leaves a record.

### SL-docs — Documentation & spec reconciliation

- **Scope**: Refresh the docs catalog, update cross-cutting documentation touched or invalidated by this phase's impl lanes, and append any post-execution amendments to phase specs whose interface freezes turned out wrong.
- **Owned files**: `.claude/docs-catalog.json`, `CHANGELOG.md`, `README.md`, `specs/phase-plans-v13.md`
- **Interfaces provided**: (none)
- **Interfaces consumed**: (none)
- **Parallel-safe**: no (terminal)
- **Depends on**: SL-1, SL-2, SL-3
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Action |
|---|---|---|---|---|
| SL-docs.1 | docs | — | `.claude/docs-catalog.json` | Rescan via `_shared/scaffold_docs_catalog.py --rescan` if present; if absent, record "docs-catalog rescan helper unavailable; manual catalog audit" in the commit message and proceed. |
| SL-docs.2 | docs | SL-docs.1 | per catalog | Decide per catalog file whether this phase changes it. CHANGELOG gets the behaviour-change entry for default-deny discovered provisioning and the new `pmcp trust` package verbs; README gets the policy `packages:` section. **SECURITY.md is deliberately NOT updated here** — the roadmap's Execution Notes assign the trust-model write-up to SEAL, once, and the TRUST plan set that precedent. Record the skip explicitly. |
| SL-docs.3 | docs | SL-docs.2 | `specs/phase-plans-v13.md` | Append `### Post-execution amendments` to the PKGID phase section if either IF-0-PKGID gate proved wrong in practice, or if the EC-PKGID-5 choice recorded below had to change during execution. |
| SL-docs.4 | verify | SL-docs.3 | — | `uv run ruff format --check src/ tests/ scripts/` plus any repo doc linters; no-op if none configured. |

## Execution Notes

- **Plan budget exceeded, justified.** This plan is ~3.5k words against the 3000-word
  budget. PKGID is the roadmap's largest phase and its critical path, it closes the
  only HIGH finding, and it is the one phase whose two single-writer files are each
  contended by a *different* sibling phase. The overage is spent on the two things a
  lane cannot safely guess: the frozen decision order in IF-0-PKGID-1, and the
  file:line evidence backing each acceptance criterion's "fails on `main`" claim.
- **Machine validators unavailable in this checkout.** `scripts/validate_plan_doc.py`
  does not exist and `phase_loop_runtime.planner_validation` is not importable from
  this worktree's interpreter (measured under both `uv run python` and system
  `python3`: `ModuleNotFoundError: No module named 'phase_loop_runtime'`). The Lane
  validation checklist was walked by hand: file ownership is disjoint, the DAG is
  acyclic (`SL-2`, `SL-3` roots → `SL-1` → `SL-4`), every `impl` follows a `test` in
  its lane, every acceptance item names a proving command, and no criterion uses a
  `-k` expression — every one names exact `::test_` functions, because a non-matching
  `-k` exits 5 and proves nothing. `## Dispatch Hints` is deliberately omitted rather
  than authored unvalidated. **Unlike the TRUST plan, the skill's advisor review
  (step 7.75) DID run here**, and produced findings folded in below; it was expected
  to be unavailable and was not.
- **Advisor findings folded in**: no `resolved_version` field is added to
  `ServerConfig`; the registry-supplied version is validated against a strict pattern
  before it is composed into argv; `validation.py` is assigned an explicit owner
  rather than left unowned in the roadmap's Key files; EC-PKGID-6 is phrased so it
  fails on unchanged `main`; and the roadmap's stale `SECURITY.md` evidence path was
  reported upward rather than silently dropped — it has since been removed from the
  roadmap, in the same PR that landed this plan.
- **Single-writer files**: `src/pmcp/tools/handlers.py` — owner **SL-1**, and this is
  the single-writer hazard of the whole v13 roadmap. It is 6274 lines and the
  roadmap's EGRESS phase writes it from **both** of its lanes (`:4796-4992`). Do not
  run SL-1 concurrently with either EGRESS lane; land one phase's handlers work
  before opening the other's. PKGID's edits are confined to `:4127-4490`
  (`provision`) and `:5513-5587` (`register_discovered_server`), disjoint *ranges*
  from EGRESS's — but git merges files, not ranges, so serialise anyway.
  `src/pmcp/cli.py` — owner **SL-1**. TRUST's merged Execution Notes state "CONSENT
  and PKGID do not write `cli.py`"; **that is now wrong** and this plan corrects it.
  The note's purpose still holds: TRUST is merged, CONSENT touches no `cli.py`, so
  SL-1 is the sole writer at execution time and no concurrent-writer edge exists.
  SL-docs.3 should record the correction as a post-execution amendment.
- **Cross-phase hazard — `src/pmcp/policy/policy.py` is NOT exclusively ours.**
  CONSENT lane C writes the same file (policy precedence and narrowing-only
  intersection) and is being planned concurrently. SL-2 adds `PackagePolicy`,
  `GatewayPolicy.packages` and `is_package_allowed` **without restructuring policy
  load or merge order**, which is CONSENT lane C's subject. Execute SL-2 and CONSENT
  lane C serially against this file; whichever lands second rebases.
  `src/pmcp/types.py` is a 1444-line hub file — SL-2's edit is confined to the policy
  models around `:958-1035`.
- **No lane touches `src/pmcp/manifest/loader.py`.** `ServerConfig` lives there and it
  is CONSENT lane A's file. An executor that "helpfully" adds a `resolved_version`
  field to `ServerConfig` has created a cross-phase conflict for no gain: the identity
  belongs in the approval store and the pin belongs in the install argv.
- **The `source == "manifest"` exemption depends on CONSENT.** EC-PKGID-3 exempts
  manifest-backed servers, which is only sound if an unapproved project overlay
  cannot inject a server into the manifest map — that is CONSENT's EC-CONSENT-6, the
  seam the roadmap's rev-2 board found. PKGID must not widen the exemption to
  compensate, and SEAL's EC-SEAL-5 is what proves the seam closed.
- **Known destructive changes**: none — every lane is purely additive except SL-3,
  which replaces one INFO log line (`installer.py:126-128`) with WARNING lines
  carrying the full argv. No file is deleted by any lane.
- **Expected add/add conflicts**: none — there is no SL-0 preamble lane;
  `provision_gate.py` and `package_approvals.py` are new modules no other lane stubs.
- **SL-0 re-exports**: not applicable — this phase adds no package `__init__.py`
  re-exports. If a later phase wants `pmcp.provision_gate` re-exported from a package
  `__init__`, use the `__getattr__` lazy form.
- **Parallelism**: SL-2 and SL-3 are DAG roots sharing no files — run them
  concurrently. SL-1 opens when SL-2's interfaces land and must not begin against a
  guessed `is_package_allowed` signature.
- **Stale-base guidance** (copy verbatim): Lane teammates working in isolated
  worktrees do not see sibling-lane merges automatically. If a lane finds its worktree
  base is pre-SL-2 (SL-1's only upstream dependency), it MUST stop and report rather
  than committing — the orchestrator will re-spawn or rebase. Silent
  `git reset --hard` or `git checkout HEAD~N -- …` in a stale worktree produces
  commits that destroy peer-lane work on `--no-ff` merge.
- **EC-PKGID-5 decision — resolve-and-pin at registration, refusal as its failure
  branch.** The roadmap offers refuse-unresolvable *or* resolve-and-pin; leaving that
  open causes lane thrash, so it is decided here. Rationale: (a) an agent-supplied
  version is still an agent-chosen label, and `is_valid_package_name` rejects
  `pkg@1.2.3` today, so "refuse without a version" would mean widening the
  agent-facing API to let the agent name the bytes — the opposite of "identity, not
  labels"; (b) TRUST SL-2 already ships `resolve_package_identity` producing
  `resolved_version`, so resolve-and-pin consumes IF-0-TRUST-2 instead of leaving it
  unused by the phase that most needs it; (c) decisively, an unpinned `npx -y pkg`
  **re-resolves at every spawn**, so an approval recorded against version X silently
  executes version Y later — pinning closes that check-then-use window at the package
  layer exactly as IF-0-TRUST-6 closes it at the file layer. The unresolvable case is
  refused at **registration**, not deferred to provision, so the agent learns
  immediately and no unpinned config is ever stored.

## Acceptance Criteria

- [ ] EC-PKGID-1 — proven by `uv run pytest -q tests/test_package_identity_gate.py::test_the_review_reproduction_does_not_spawn_npx_for_an_arbitrary_package tests/test_package_identity_gate.py::test_the_refusal_names_the_package_and_the_approval_command`, falsified by driving the review's exact reproduction: a policy with `servers.allowlist == ["internal-approved-tool"]`, then `register_discovered_server(server_name="internal-approved-tool", package="totally-arbitrary-evil-package")`, then `provision(server_name="internal-approved-tool")` with `JobManager.start_install` replaced by a recorder — assert the recorder was **never called**, `ProvisionOutput.ok is False`, and the message contains both `"totally-arbitrary-evil-package"` and `"pmcp trust approve-package"`. On unchanged `main` this fails: the recorder is called with `["npx","-y","totally-arbitrary-evil-package"]` and `ok=True, status="started"`.
- [ ] EC-PKGID-2 — proven by `uv run pytest -q tests/test_package_identity_gate.py::test_policy_denylist_blocks_an_approved_package tests/test_package_identity_gate.py::test_policy_package_allowlist_permits_provision_without_a_recorded_approval tests/test_package_identity_gate.py::test_package_denylist_beats_a_recorded_approval tests/test_policy_package_identifiers.py::test_evaluate_package_policy_denylist_beats_allowlist tests/test_policy_package_identifiers.py::test_a_policy_with_no_packages_section_returns_unspecified`, falsified by a policy whose `servers.allowlist` permits the *name* while `packages.denylist` names the package, asserting provision is refused — deny wins over both the name allowlist and a recorded approval; and at the policy layer by asserting an unconfigured `packages` section returns `"unspecified"` rather than a default-allow `True`. Fails on `main`: `GatewayPolicy` has no `packages` section.
- [ ] EC-PKGID-3 — proven by `uv run pytest -q tests/test_package_identity_gate.py::test_discovered_provisioning_is_denied_without_opt_in tests/test_package_identity_gate.py::test_a_manifest_backed_server_provisions_unchanged tests/test_package_identity_gate.py::test_source_is_taken_from_the_lookup_path_not_from_server_config`, falsified by provisioning a freshly discovered server with no approval and no policy entry and asserting refusal, while a manifest-backed name provisions with no approval at all; and by a discovered config carrying `declared_capabilities=["manifest"]` still being treated as `source="discovered"`. Fails on `main`: the first case succeeds today.
- [ ] EC-PKGID-4 — proven by `uv run pytest -q tests/test_install_argv_logging.py::test_start_install_logs_rendered_argv_at_warning tests/test_install_argv_logging.py::test_legacy_install_server_logs_rendered_argv_at_warning tests/test_install_argv_logging.py::test_verify_installation_logs_rendered_argv_at_warning tests/test_install_argv_logging.py::test_argv_is_logged_even_when_the_spawn_fails tests/test_install_argv_logging.py::test_a_credential_bearing_argument_is_redacted_but_the_package_identity_is_not`, falsified with `caplog.at_level(logging.WARNING)` asserting the rendered argv appears at WARNING before the spawn — executable verbatim and `name@resolved_version` present — that it still appears when `create_subprocess_exec` raises `FileNotFoundError`, and that an argument outside the frozen safe shape (e.g. `--token=sk-live-…`) is rendered `<redacted>` while the package identity in the same argv is not. Fails on `main`: the only log is INFO and literally contains `<args redacted>` (`installer.py:126-128`).
- [ ] EC-PKGID-5 — proven by `uv run pytest -q tests/test_package_identity_gate.py::test_a_resolvable_registration_records_the_version_and_pins_the_install_argv tests/test_package_identity_gate.py::test_an_unresolvable_registration_is_refused_at_registration tests/test_package_identity_gate.py::test_a_resolved_version_that_fails_validation_is_refused tests/test_policy_package_identifiers.py::test_a_registry_supplied_version_with_metacharacters_is_rejected`, falsified by asserting the **chosen** branch: a resolvable spec registers with `resolved_version` recorded and `install_command == ["npx","-y","pkg@<version>"]` for every platform key **and** the runtime `args == ["-y","pkg@<version>"]` (the argv `client/manager.py:2349` actually spawns), an unresolvable spec returns `registered=False` from `register_discovered_server` itself, and a registry-returned version failing `is_valid_package_version` is refused rather than composed into argv. Fails on `main`: registration stores `["npx","-y","pkg"]` unpinned and never resolves a version.
- [ ] EC-PKGID-6 — proven by `uv run pytest -q tests/test_package_identity_gate.py::test_an_approved_package_provisions_after_the_refusal tests/test_package_identity_gate.py::test_a_manifest_backed_server_provisions_unchanged tests/test_package_approvals.py::test_a_store_read_failure_denies_rather_than_raises`, falsified by the round trip: provision is refused, `approve_package(identity)` is recorded, the same provision then reaches `start_install` with the pinned argv. There is **nothing durable to migrate** — `_discovered_server_configs` is a process-local instance dict (`handlers.py:1126`) and `server.py:387-388` merely dispatches, so no persisted registration survives a restart today. Grandfathering is therefore the explicit recorded decision the operator makes once, prompted by a refusal that names the exact command (EC-PKGID-1), and manifest-backed servers — the only servers that *do* persist — are untouched by construction. Assumption 5 is satisfied in its letter: the change is loud, not silent. Regression evidence for the untouched operator is the suite-count comparison in `## Verification`.

## Verification

```bash
uv run pytest -q tests/test_package_identity_gate.py tests/test_package_approvals.py \
                tests/test_policy_package_identifiers.py tests/test_install_argv_logging.py
uv run pytest -q tests/test_tools.py tests/test_provision_validation.py tests/test_policy.py \
                tests/test_manifest.py tests/test_manifest_provision.py tests/test_trust_cli.py
uv run pytest -q tests/                  # compare counts to the pre-phase baseline, same dir
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
uv run python -c "import pmcp.provision_gate, pmcp.package_approvals"   # both importable

# The gate covers every reachable install spawn: exactly one production callsite.
rg -n 'await \w+\.start_install\(' src/     # MUST be only handlers.py:4483

# PKGID does not touch CONSENT's loaders.
git diff --name-only origin/main..HEAD -- src/pmcp/manifest/loader.py src/pmcp/config/loader.py
# ^ MUST be empty. A non-empty result means a lane crossed the phase boundary.
```

Host note: `/tmp/package.json` makes ~107 npm-identity tests fail on some hosts
(`tests/conftest.py`); compare the same command from the same directory before and
after, never against a clean-machine expectation.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `src/pmcp/tools/handlers.py`, `src/pmcp/policy/policy.py`, `src/pmcp/manifest/installer.py`
- evidence paths: `tests/test_package_identity_gate.py`, `CHANGELOG.md`
- redaction posture: `metadata_only`
- downstream handling: none — `SECURITY.md` was removed from this phase's roadmap evidence paths in the same PR that landed this plan; the trust-model write-up belongs to SEAL. No skip needs recording, because the roadmap no longer asks for it.

## Execution Policy

- default: effort=medium
- SL-1: effort=high, reason=fail-closed ordering where a wrong default grants arbitrary code execution
- SL-2: effort=medium, reason=policy semantics shared with a concurrently planned phase
- SL-3: effort=low, reason=three log statements and their tests
- SL-4: effort=minimal, reason=docs sweep only
