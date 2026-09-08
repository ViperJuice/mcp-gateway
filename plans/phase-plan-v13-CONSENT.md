---
phase_loop_plan_version: 1
phase: CONSENT
roadmap: specs/phase-plans-v13.md
roadmap_sha256: 084b23212b9df39888f3772476dc7895bc99ff3d837d003a4c60cde4f2da2d14
---

# PHASE-2-CONSENT: Project-scoped configuration requires consent

## Context

CONSENT is the first phase that **wires a caller**. TRUST froze the store and
deliberately connected nothing; CONSENT connects three loaders to it and changes
one behaviour an existing operator can feel.

What exists today, verified in this checkout:

- **Manifest overlay** — `_find_project_manifest` (`manifest/loader.py:617-647`)
  walks up from `Path.cwd()` to the nearest `.pmcp/manifest.yaml`, checks only
  that a symlink does not escape the tree, and trusts the content.
  `_overlay_manifest_paths` (`:650-676`) returns `user < project < env`, and
  `load_manifest` applies each with `servers.update(overlay_servers)`
  (`:801`) — whole-entry replace of `command`, `args`, `install`,
  `requires_api_key`. Because `update()` also **inserts** unknown keys, an
  overlay can add a server that never shipped, and `manifest.get_server(name)`
  — the exact predicate `tools/handlers.py:4279` uses to decide a server is
  manifest-backed — then answers for it. That is the seam EC-CONSENT-6 names.
- **Project `.mcp.json`** — read at **four independent sites** in
  `config/loader.py`: `_iter_config_source_paths` (`:281-283`, feeding
  `load_config_sources` → `get_startup_policy` and
  `registry_allow_private_from_config`), `load_configs` (`:796-813`),
  `load_disabled_auto_start` (`:881-883`), and `load_enabled_auto_start`
  (`:925-927`). Gating only `load_configs` leaves three readers fail-open.
- **Policy discovery** — `DEFAULT_POLICY_PATHS` (`policy/policy.py:41-46`) lists
  the two project-relative names **before** the two `~/.claude/` ones, and
  `PolicyManager.__init__` (`:71-75`) loops `if default_path.exists(): load;
  break`. First match wins, project first, and the default `GatewayPolicy()` is
  allow-all — S-11 exactly. An explicit `--policy` sets `_explicit_policy` and
  skips discovery entirely (`:67-69`).
- `is_scoped_advisor_policy` (`:209`) requires `_explicit_policy`, so a project
  policy can never activate the scoped-advisor profile. No interaction.
- `policy_digest` (`:205-207`) hashes only `self._policy`. Any composition added
  here must be inside that digest or a project policy becomes invisible to
  telemetry.

Two constraints shape every lane:

1. **Read once, gate those bytes, parse those bytes.** IF-0-TRUST-1's
   `is_approved(path, content: bytes)` is defeated if a loader gates one read
   and parses a second. Today all three loaders read from disk directly
   (`open(path)` at `manifest/loader.py:696`, `path.read_text()` at
   `config/loader.py:308`, `policy_path.read_text()` at `policy/policy.py:100`).
   Each must be restructured to take bytes from the gate.
2. **User and env scope stay ungated.** `~/.pmcp/manifest.yaml`, the user
   `.mcp.json` paths, `$PMCP_MANIFEST_PATH`, `$PMCP_CONFIG` and `--policy` are
   operator-supplied, not repository-supplied. Gating them would break working
   setups and fail EC-CONSENT-5.

## Interface Freeze Gates

- [ ] IF-0-CONSENT-1 — the single project-source gate, `src/pmcp/project_consent.py`. `ProjectSourceKind = Literal["project_manifest", "project_mcp_json", "project_policy"]`; frozen `ConsentDecision(allowed: bool, path: Path, kind: ProjectSourceKind, reason: str, remediation: str)` where `reason` is the closed vocabulary `{"approved", "no_record", "content_changed", "unreadable"}` and `remediation` is the exact shell string `pmcp trust approve <absolute path>` (empty only when `allowed` is True); `read_and_gate(path: Path, kind: ProjectSourceKind) -> tuple[bytes | None, ConsentDecision]` performs **exactly one** read and gates those bytes, returning the bytes only when `allowed` (the caller parses the returned bytes and MUST NOT re-open the path); `gate_bytes(path: Path, content: bytes, kind: ProjectSourceKind) -> ConsentDecision` for callers that already hold bytes; `log_refusal(decision: ConsentDecision, logger: logging.Logger) -> None` emits the single WARNING format naming `decision.remediation`. Every failure mode — unreadable file, store I/O error, any exception from the trust store — returns `allowed=False`; the gate never raises to a caller that might read a raise as permission.
- [ ] IF-0-CONSENT-2 — **conjunctive** policy composition, not list intersection. `PolicyManager` gains a private `_project_policy: GatewayPolicy | None`; every predicate returns `user_result and (project is None or project_result)`, so a server/tool/resource/prompt is allowed only if **both** policies allow it. List intersection of glob *strings* is rejected as wrong: user `github*` ∩ project `github-mcp` is empty although `github-mcp` must stay allowed. Denylists compose by union (implied by conjunction); `get_max_tools_per_server`, `get_max_output_bytes`, `get_max_output_tokens` return `min(user, project)`; `redact_secrets` compiles the union of the two **effective** pattern sets, where "effective" means each policy's `redaction.patterns` **or** `DEFAULT_REDACTION_PATTERNS` when that list is empty. The naive list union is rejected as a widening: `_compile_redaction_patterns` (`policy/policy.py:142`) does `patterns or DEFAULT_REDACTION_PATTERNS`, so with a common empty user list, `[] + ["x"]` is truthy and would **drop every default pattern** — a repository file silently reducing secret redaction, the S-11 class again. Limits take `min()` over the user's **effective** value and the project's
**explicitly-set** value only (`model_fields_set`), asymmetrically. Neither symmetric rule is
safe: min-over-effective lets a project policy that never mentions `limits` drag a raised user
limit back down via its Pydantic defaults (100 / 50000 / 4000), while min-over-explicitly-set-on-
both-sides is **worse — it widens**. If the user never set `max_tools_per_server` (effective 100)
and an approved project sets `1000`, the only explicitly-set value is the project's, so `min()`
over that set returns 1000 and a repository file raises the operator's ceiling. That is the S-11
class this phase exists to close, arriving through the limits path. The user's effective value is
therefore always a term; only the project's unset fields are excluded. **The boolean rule above applies only to predicates that return a bool.** PKGID adds
`evaluate_package_policy -> Literal["denied","allowed","unspecified"]`; all three are truthy
strings, so `user_result and project_result` returns the *second* operand and a user
`"denied"` composed with a project `"allowed"` evaluates to `"allowed"` — a repository file
turning an operator denylist into an allow. Tri-state predicates therefore compose by an
explicit rule frozen here, not by `and`: if **either** side is `"denied"` the result is
`"denied"`; a project `"allowed"` never independently grants, it can only fail to deny,
leaving the user's verdict to decide; otherwise the result is the user's verdict. This is
part of the freeze because a lane implementing the boolean rule literally would ship the
widening. `policy_digest` hashes both policies so a project policy is never invisible. `explicit_policy` and `is_scoped_advisor_policy` are unchanged: an explicit `--policy` skips discovery and composition entirely.

## Lane Index & Dependencies

SL-1 — Project-source consent gate
  Depends on: (none)
  Blocks: SL-2, SL-3, SL-4, SL-5
  Parallel-safe: yes

SL-2 — Manifest overlay consent
  Depends on: SL-1
  Blocks: SL-5
  Parallel-safe: yes

SL-3 — Project `.mcp.json` consent
  Depends on: SL-1
  Blocks: SL-5
  Parallel-safe: yes

SL-4 — Policy consent and narrowing
  Depends on: SL-1
  Blocks: SL-5
  Parallel-safe: yes

SL-5 — Documentation & spec reconciliation
  Depends on: SL-1, SL-2, SL-3, SL-4
  Blocks: (none)
  Parallel-safe: no

## Lanes

### SL-1 — Project-source consent gate

- **Scope**: The one decision surface all three loaders call, wrapping TRUST's store so no loader hashes, reads twice, or formats a refusal on its own.
- **Owned files**: `src/pmcp/project_consent.py`, `tests/test_project_consent_gate.py`, `tests/conftest.py`
- **Interfaces provided**: `ProjectSourceKind`, `ConsentDecision`, `read_and_gate`, `gate_bytes`, `log_refusal`
- **Interfaces consumed**: `pmcp.trust_store.is_approved` (TRUST SL-1, IF-0-TRUST-1)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-1.1 | test | — | `tests/test_project_consent_gate.py` | **exactly these names**, because acceptance criteria address them individually: `test_an_unrecorded_path_is_refused`, `test_content_change_after_approval_is_not_approved`, `test_an_unreadable_source_is_refused_not_raised`, `test_a_store_error_is_refused_not_raised`, `test_read_and_gate_opens_the_path_exactly_once`, `test_read_and_gate_returns_none_bytes_when_refused`, `test_remediation_is_the_absolute_path_trust_approve_command`, `test_log_refusal_emits_one_warning_naming_the_remediation` | `uv run pytest -q tests/test_project_consent_gate.py` |
| SL-1.2 | impl | SL-1.1 | `src/pmcp/project_consent.py` | — | — |
| SL-1.3 | impl | SL-1.2 | `tests/conftest.py` | — | — |
| SL-1.4 | verify | SL-1.3 | `src/pmcp/project_consent.py`, `tests/conftest.py`, `tests/test_project_consent_gate.py` | all SL-1 tests | `uv run pytest -q tests/test_project_consent_gate.py && uv run mypy src/` |

`test_read_and_gate_opens_the_path_exactly_once` is the TOCTOU guard: monkeypatch
`Path.open`/`read_bytes` with a counting wrapper and assert exactly one call, so a
future edit cannot reintroduce a gate-then-reread.

**SL-1.3 is the store-isolation seam every other lane needs.** `tests/conftest.py`
gains an autouse fixture redirecting the trust store into `tmp_path` so no test
reads or writes the developer's real `~/.config/pmcp` (the T-08 hazard the
2026-09-01 review already flagged for another file). It **must not** auto-approve
anything: an autouse approval would make every refusal test in SL-2/3/4 vacuously
green. Lanes record approvals explicitly per test. See TRUST gap 5 in Execution
Notes — IF-0-TRUST-1 froze `trust_store_path()` but not how a test relocates it,
so SL-1 owns choosing that seam and publishing it.

### SL-2 — Manifest overlay consent

- **Scope**: Gate the project `.pmcp/manifest.yaml` overlay so an unapproved file contributes neither a replacement nor an addition, leaving user and env overlays untouched.
- **Owned files**: `src/pmcp/manifest/loader.py`, `tests/test_project_source_consent_manifest.py`, `tests/test_manifest_overlay.py`
- **Interfaces provided**: (none — behaviour change inside `load_manifest`)
- **Interfaces consumed**: `read_and_gate`, `log_refusal`, `ConsentDecision` (SL-1)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-2.1 | test | — | `tests/test_project_source_consent_manifest.py` | `test_unapproved_overlay_does_not_replace_shipped_server`, `test_unapproved_overlay_cannot_add_a_new_server`, `test_an_added_server_from_an_unapproved_overlay_is_not_manifest_backed`, `test_unapproved_overlay_skip_logs_the_trust_approve_command`, `test_approved_overlay_is_applied`, `test_editing_an_approved_overlay_revokes_it`, `test_unapproved_overlay_server_env_patch_is_not_applied`, `test_user_scope_overlay_is_applied_without_any_trust_record`, `test_env_scope_manifest_path_is_applied_without_any_trust_record` | `uv run pytest -q tests/test_project_source_consent_manifest.py` |
| SL-2.2 | impl | SL-2.1 | `src/pmcp/manifest/loader.py` | — | — |
| SL-2.3 | verify | SL-2.2 | `src/pmcp/manifest/loader.py`, `tests/test_project_source_consent_manifest.py` | all SL-2 tests | `uv run pytest -q tests/test_project_source_consent_manifest.py tests/test_manifest.py tests/test_manifest_overlay.py tests/test_manifest_provision.py && uv run mypy src/` |

The gate is applied in `_overlay_manifest_paths`/`load_manifest` **before**
`servers.update(...)` (`:801`), not after: an unapproved overlay must contribute
nothing at all, so `load_manifest().get_server("<added>")` returns `None` and
PKGID's manifest-backed exemption at `tools/handlers.py:4279` cannot fire on it.
`_load_overlay_file` takes bytes from `read_and_gate` instead of `open(path)`.
This lane also owns the required update to `tests/test_manifest_overlay.py`:
`test_project_overrides_user_overrides_shipped` (`:90-123`) asserts an
**unapproved** project overlay replaces a shipped server's command — precisely
what EC-CONSENT-1 now forbids. It must record an approval in its fixture. See the
EC-CONSENT-5 roadmap defect in Execution Notes.

### SL-3 — Project `.mcp.json` consent

- **Scope**: Gate the project `.mcp.json` at every one of its four read sites, leaving user and custom/`$PMCP_CONFIG` sources ungated.
- **Owned files**: `src/pmcp/config/loader.py`, `tests/test_project_source_consent_config.py`, `tests/test_config_loader.py`
- **Interfaces provided**: (none — behaviour change inside the project branch of each reader)
- **Interfaces consumed**: `read_and_gate`, `log_refusal`, `ConsentDecision` (SL-1)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-3.1 | test | — | `tests/test_project_source_consent_config.py` | `test_unapproved_project_mcp_json_is_not_applied_by_load_configs`, `test_unapproved_project_mcp_json_is_not_applied_by_load_config_sources`, `test_unapproved_project_mcp_json_is_not_applied_by_load_disabled_auto_start`, `test_unapproved_project_mcp_json_is_not_applied_by_load_enabled_auto_start`, `test_unapproved_project_mcp_json_does_not_set_allow_private_registry`, `test_unapproved_project_mcp_json_skip_logs_the_trust_approve_command`, `test_approved_project_mcp_json_is_applied`, `test_editing_an_approved_project_mcp_json_revokes_it`, `test_user_mcp_json_is_applied_without_any_trust_record`, `test_custom_config_path_is_applied_without_any_trust_record` | `uv run pytest -q tests/test_project_source_consent_config.py` |
| SL-3.2 | impl | SL-3.1 | `src/pmcp/config/loader.py` | — | — |
| SL-3.3 | verify | SL-3.2 | `src/pmcp/config/loader.py`, `tests/test_project_source_consent_config.py` | all SL-3 tests | `uv run pytest -q tests/test_project_source_consent_config.py tests/test_config_loader.py && uv run mypy src/` |

Gate once in a single private helper the project branch of all four readers
calls, rather than four copies. The four per-reader tests exist because the
roadmap's phrasing ("an unapproved project `.mcp.json` is not applied") invites
gating only `load_configs`, which would leave three readers fail-open.
This lane also owns the required update to `tests/test_config_loader.py`, whose
project-fixture tests (`test_loads_project_config` `:243`,
`test_merges_configs_with_precedence` `:292`, `test_normalizes_relative_paths`
`:374`, `test_keeps_remote_entries` `:395` and siblings) create a `tmp_path`
`.mcp.json` and assert it loads. Each must record an approval in its fixture.

### SL-4 — Policy consent and narrowing

- **Scope**: Stop a project policy shadowing the user policy — gate whether it is read at all, and compose it conjunctively when it is.
- **Owned files**: `src/pmcp/policy/policy.py`, `tests/test_project_source_consent_policy.py`, `tests/test_policy_fail_open.py`
- **Interfaces provided**: IF-0-CONSENT-2 composition semantics
- **Interfaces consumed**: `read_and_gate`, `log_refusal`, `ConsentDecision` (SL-1)
- **Parallel-safe**: yes
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Tests owned | Test command |
|---|---|---|---|---|---|
| SL-4.1 | test | — | `tests/test_project_source_consent_policy.py` | `test_unapproved_project_policy_is_not_read`, `test_unapproved_project_policy_does_not_shadow_the_user_policy`, `test_unapproved_project_policy_refusal_names_the_trust_approve_command`, `test_project_policy_cannot_allow_what_user_policy_denies`, `test_project_policy_cannot_widen_a_user_allowlist`, `test_project_policy_denial_still_applies_on_top_of_user_policy`, `test_project_policy_limits_take_the_minimum_of_the_user_effective_and_project_explicit_values`, `test_a_project_limit_cannot_raise_a_limit_the_user_never_set`, `test_project_policy_unset_limits_do_not_lower_a_raised_user_limit`, `test_project_redaction_patterns_extend_rather_than_replace_defaults`, `test_policy_digest_covers_the_project_policy`, `test_explicit_policy_path_skips_discovery_and_composition`, `test_editing_an_approved_project_policy_revokes_it`, `test_user_policy_alone_is_unchanged` | `uv run pytest -q tests/test_project_source_consent_policy.py` |
| SL-4.2 | impl | SL-4.1 | `src/pmcp/policy/policy.py` | — | — |
| SL-4.3 | verify | SL-4.2 | `src/pmcp/policy/policy.py`, `tests/test_project_source_consent_policy.py` | all SL-4 tests | `uv run pytest -q tests/test_project_source_consent_policy.py tests/test_policy.py tests/test_policy_fail_open.py && uv run mypy src/` |

`PolicyManager.__init__`'s discovery loop (`:71-75`) stops breaking on first
match: it resolves the first existing **user**-scoped path as the base policy and
the first existing **project**-scoped path as the candidate overlay. Consiliency/pmcp#202's
fail-closed rule is preserved for both — a project policy that parses but fails
validation still refuses to start, *after* the consent gate has admitted it.
This lane also owns the required update to `tests/test_policy_fail_open.py`: the
whole module exercises *discovered* (project-scoped) policy files under
`tmp_path`, so every case needs an approval recorded, and two encode the
behaviour SL-4 deliberately changes — `test_higher_priority_invalid_policy_does_not_fall_through`
(`:171`) and `test_default_paths_follow_the_cwd_at_construction` (`:303`) both
assert first-match-wins ordering. Updating those is asserting the new contract,
not weakening the old one.

### SL-docs — Documentation & spec reconciliation

- **Scope**: Refresh the docs catalog, update cross-cutting documentation touched or invalidated by this phase's impl lanes, and append post-execution amendments to the roadmap where this phase's contracts diverged from it.
- **Owned files**: `.claude/docs-catalog.json`, `CHANGELOG.md`, `specs/phase-plans-v13.md`
- **Interfaces provided**: (none)
- **Interfaces consumed**: (none)
- **Parallel-safe**: no (terminal)
- **Depends on**: SL-1, SL-2, SL-3, SL-4
- **Tasks**:

| Task ID | Type | Depends on | Files in scope | Action |
|---|---|---|---|---|
| SL-docs.1 | docs | — | `.claude/docs-catalog.json` | Rescan via `_shared/scaffold_docs_catalog.py --rescan` if present; if absent, record "docs-catalog rescan helper unavailable; manual catalog audit" in the commit message and proceed. |
| SL-docs.2 | docs | SL-docs.1 | `CHANGELOG.md`, per catalog | Write the CHANGELOG entry **SL-4's behaviour change first** — it is the one an existing operator can feel (a project policy that used to replace the user policy now only narrows it, and only when approved). **SECURITY.md is deliberately NOT updated here**: the roadmap assigns the trust-model write-up to SEAL, once. Record that skip explicitly. |
| SL-docs.3 | docs | SL-docs.2 | `specs/phase-plans-v13.md` | Append `### Post-execution amendments` to the CONSENT section recording: (a) the evidence path `tests/test_project_source_consent.py` was split into four per-lane files (a single file cannot be disjointly owned by three lanes) — amend to `tests/test_project_consent_gate.py`, `tests/test_project_source_consent_manifest.py`, `tests/test_project_source_consent_config.py`, `tests/test_project_source_consent_policy.py`; (b) the phase decomposed into 4 impl lanes, not 3, because IF-0-CONSENT-1's "single call every loader uses" is itself a file all three loaders consume; (c) EC-CONSENT-5's proof clause is superseded — the git-diff-empty guard names only `tests/test_policy.py`, `tests/test_manifest.py`, `tests/test_manifest_provision.py`, and the three project-fixture suites are updated by their owning lanes; (d) any IF-0-TRUST-1 assumption in Execution Notes that proved wrong in practice. |
| SL-docs.4 | verify | SL-docs.3 | — | `uv run ruff format --check src/ tests/ scripts/` plus any repo doc linters; no-op if none configured. |

## Execution Notes

- **Plan budget exceeded — justification.** This document is ~4.4k words against
  the 3000-word GOVLEAN budget. The overage is concentrated in two places that
  are load-bearing rather than decorative: (a) the seven acceptance criteria each
  enumerate their exact pytest node ids and a per-criterion falsifier with the
  `file:line` on unchanged `main` that the falsifier defeats — a `-k` expression
  or a prose falsifier would fit the budget and prove nothing; (b) the four
  IF-0-TRUST-1 underspecifications below, which are findings the phase must
  report rather than resolve by guessing. Trimming either would trade a measured
  contract for a shorter one. The advisor round added ~900 words of measured
  findings (the EC-CONSENT-5 defect, the redaction-union widening, TRUST gap 5);
  those are the highest-value words in the document. For scale: TRUST is ~2.0k
  words for 6 criteria and 3 lanes; CONSENT covers 7 criteria and 5 lanes.
- **Machine validators unavailable in this checkout — measured, not assumed.**
  `ls scripts/validate_plan_doc.py` → no such file. `python3 -c "from
  phase_loop_runtime.planner_validation import validate_plan_dispatch_hints"` and
  the same under `uv run` → `ModuleNotFoundError: No module named
  'phase_loop_runtime'`. The skill's advisor-review step (7.75) was attempted and
  **timed out on the first attempt and succeeded on retry**, returning five
  findings, all folded in: EC-CONSENT-5's proof clause collides with the gating
  criteria (the blocking one — measured by grep and recorded as a roadmap defect
  above); IF-0-CONSENT-2's redaction "union" as first written would have dropped
  `DEFAULT_REDACTION_PATTERNS` whenever the user list was empty, a widening of
  exactly the S-11 class; the limits `min()` rule needed an explicit
  effective-vs-explicitly-set choice; TRUST leaves no frozen test seam for store
  location (gap 5); and the two weakest SL-4 test names would have passed the
  broken implementations they were meant to catch, so both were renamed. This
  plan has had **no *automated* validation** — only that advisor round plus the
  Lane validation checklist walked by hand: file
  ownership is disjoint (`project_consent.py` / `manifest/loader.py` /
  `config/loader.py` / `policy/policy.py`, one test file each), the DAG is
  acyclic, every `impl` follows a `test` in its lane, every acceptance item names
  exact test functions and a proving command, and no criterion rests on a `grep`.
  `## Dispatch Hints` is deliberately **omitted** rather than authored
  unvalidated. Treat these freezes as less hardened than TRUST's, which at least
  got a board round.
- **Cross-phase single-writer hazard — `src/pmcp/policy/policy.py`.** Owned here
  by **SL-4**, and also written by **PKGID lane B** (package identifiers in
  policy) in the sibling phase. CONSENT does **not** own this file exclusively.
  Per the roadmap DAG note, execute SL-4 and PKGID's policy lane serially against
  it, or land one phase's policy lane before opening the other's. SL-4 adds
  `_project_policy` and makes every predicate conjunctive; PKGID's lane adds
  package-identifier predicates. If PKGID lands first, SL-4 must compose the new
  package predicates too — a package predicate that reads only
  `self._policy` would let an approved project policy widen package permissions,
  **but it must NOT be composed with the boolean rule above.** PKGID's
  `evaluate_package_policy` is tri-state (`"denied"` / `"allowed"` / `"unspecified"`),
  and all three are truthy strings, so `user_result and project_result` returns the
  *second* operand: composing a user `"denied"` with a project `"allowed"` yields
  `"allowed"` — a user denial silently inverted by a repository file. The tri-state
  composition rule is separate and explicit: if either side is `"denied"` the result is
  `"denied"`; a project `"allowed"` never independently grants (it can only fail to
  deny, leaving the user's own verdict to decide); otherwise the result is the user's
  verdict. This must be covered by a test with both phases integrated, not by either
  phase alone,
  silently reopening EC-CONSENT-3 through a door PKGID built.
- **Cross-phase single-writer — docs.** `CHANGELOG.md` and
  `specs/phase-plans-v13.md` are owned by SL-docs here and by PKGID's docs lane
  there. Both are append-mostly; serialise the two docs lanes or expect a
  textual conflict at the CHANGELOG head.
- **Underspecified by IF-0-TRUST-1 — resolve before SL-1 starts.** These are
  reported as findings, not papered over:
  1. **Record-path residency is ambiguous.** IF-0-TRUST-1 says the store
     "refuses a path inside the current checkout … resolving symlinks before
     comparing against the checkout root". Read strictly that governs
     `trust_store_path()` only. But it sits in the same sentence as `record(path,
     …)`, and a TRUST implementer could apply the checkout refusal to the
     **recorded** path. CONSENT requires the opposite reading — every path it
     records is by definition inside a checkout. **Stated assumption**: the
     residency check constrains the store file's own location, never the `path`
     argument of `record` / `is_approved` / `revoke`. If TRUST shipped the strict
     reading, all four lanes are blocked and TRUST needs an amendment, not a
     workaround here.
  2. **`scope` has no frozen vocabulary and cannot participate in matching.**
     IF-0-TRUST-1 fixes a closed vocabulary for `decision` but leaves `scope:
     str` open, and `is_approved(path, content)` accepts no scope argument.
     CONSENT therefore treats `scope` as descriptive metadata only and records
     the literal `"project"`. If a TRUST implementer made `is_approved`
     scope-sensitive, every lane here breaks — and `pmcp trust approve` would
     have to grow a `--scope` flag it does not have.
  3. **`pmcp trust approve` semantics are named but not specified.** Every
     refusal message this phase emits names `pmcp trust approve <absolute
     path>`. EC-TRUST-4 guarantees the verb exists; it does not guarantee the
     verb accepts an absolute path outside cwd, nor what it records for `scope`.
     **Stated assumption**: absolute path accepted, current bytes hashed,
     `decision="approved"`, `scope="project"`. If any of those differs, this
     phase's refusal messages name a command that does not do what they claim.
  4. **No bulk approve.** An operator cloning a repo shipping all three project
     files runs `pmcp trust approve` three times. Acceptable for this phase;
     flagged for SEAL's UX write-up.
  5. **No frozen test seam for store location.** Every SL-2/3/4 test must record
     approvals without touching the developer's real `~/.config/pmcp`.
     IF-0-TRUST-1 froze `trust_store_path()` but not how a test relocates it — an
     env var? a monkeypatch target? a module attribute like
     `DEFAULT_POLICY_PATHS`? **Stated assumption**: TRUST exposes
     `trust_store_path()` as a module-level function monkeypatchable from
     `pmcp.trust_store`, and SL-1.3 builds the conftest fixture on that. If TRUST
     resolved the path inline at each call site instead, SL-1 must add the seam,
     which is a write into `src/pmcp/trust_store.py` — a file this phase does not
     own and whose `## Verification` block asserts is untouched. Resolve before
     SL-1 starts.
- **Roadmap defect — EC-CONSENT-5's proof clause contradicts EC-CONSENT-1, -2 and
  -7.** Measured, not inferred: `tests/test_manifest_overlay.py:90`
  (`test_project_overrides_user_overrides_shipped`) asserts an unapproved project
  overlay replaces a shipped server's command; `tests/test_config_loader.py` has
  ~8 tests creating a `tmp_path` `.mcp.json` and asserting it loads; the whole of
  `tests/test_policy_fail_open.py` exercises discovered project-scoped policy
  files. Those three suites therefore **cannot** "pass unmodified" once the gates
  land — the roadmap's proof clause demands the phase both refuse unapproved
  project sources and keep green the tests asserting they apply. The criterion's
  *text* ("an operator with **no project files** sees byte-identical behaviour")
  is sound; only the proof clause overreaches, because those suites are not an
  operator with no project files. **Resolution**: the git-diff-empty guard
  applies to `tests/test_policy.py`, `tests/test_manifest.py` and
  `tests/test_manifest_provision.py` (verified: zero project-source fixtures);
  the other three are updated to record an approval in their fixtures and are
  assigned owners (SL-2, SL-3, SL-4 respectively) so the executor's
  `phase_owned_dirty` check does not fail closed. Rejected alternative: exempting
  an explicitly-passed `project_root` from the gate — `server.py` plumbs
  `--project-root` through that path, so the carve-out would gut EC-CONSENT-2.
  SL-docs.3 amends the roadmap.
- **Underspecified by the roadmap.** (a) "A narrowing-only rule for project
  scope" never defined *how* to narrow; IF-0-CONSENT-2 chooses conjunctive
  evaluation and states why list intersection is wrong. That is a design decision
  CONSENT is making, not one it inherited. (b) EC-CONSENT-2's singular phrasing
  understates the work: there are four project-`.mcp.json` read sites, not one.
- **Deliberately NOT gated (explicit non-goal).** `_load_local_mcp_json`
  (`src/pmcp/cli.py:1875-1889`, called at `:2016`) reads the project `.mcp.json`
  for diagnostic mode signals. It applies no config and executes nothing, and
  `cli.py` is TRUST SL-3's file — gating it here would create a cross-phase write
  for no security gain. Recorded so a reviewer does not read it as an oversight.
- **EC-CONSENT-5 is the phase's only non-regression criterion** and cannot fail
  on unchanged `main` by construction — it asserts today's behaviour survives.
  Its real instrument is a **mutation** falsifier: the
  `*_without_any_trust_record` tests in SL-2 and SL-3 fail against any
  implementation that over-gates user, env, or custom scope, and the `git diff`
  emptiness check fails if a lane edited an existing suite to make it pass. Every
  other criterion (EC-CONSENT-1, -2, -3, -4, -6, -7) fails on unchanged `main`;
  each falsifier below names why.
- **Known destructive changes**: none — no lane deletes a file. SL-4 removes the
  `break` in `PolicyManager.__init__`'s discovery loop, which is a behaviour
  change, not a deletion, and is the CHANGELOG entry SL-docs.2 sequences first.
- **Expected add/add conflicts**: none — SL-1 creates a new module no other lane
  stubs; SL-2/3/4 each modify a distinct pre-existing file.
- **SL-0 re-exports**: not applicable — this phase adds no package `__init__.py`
  re-exports. If a later phase wants `pmcp.project_consent` re-exported from a
  package `__init__`, use the `__getattr__` lazy form.
- **Parallelism**: SL-1 is the only DAG root. SL-2, SL-3 and SL-4 open together
  the moment SL-1's interfaces land and share no files — run all three
  concurrently. None may begin against a guessed signature.
- **Stale-base guidance** (copy verbatim): Lane teammates working in isolated
  worktrees do not see sibling-lane merges automatically. If a lane finds its
  worktree base is pre-SL-1, it MUST stop and report rather than committing — the
  orchestrator will re-spawn or rebase. Silent `git reset --hard` or `git
  checkout HEAD~N -- …` in a stale worktree produces commits that destroy
  peer-lane work on `--no-ff` merge.

## Acceptance Criteria

- [ ] EC-CONSENT-1 — proven by `uv run pytest -q tests/test_project_source_consent_manifest.py::test_unapproved_overlay_does_not_replace_shipped_server tests/test_project_source_consent_manifest.py::test_unapproved_overlay_skip_logs_the_trust_approve_command`, falsified by writing a `.pmcp/manifest.yaml` that replaces a shipped server's `command` with a sentinel, recording no approval, and asserting `load_manifest().get_server(<shipped>).command` is still the shipped value while a WARNING contains the literal `pmcp trust approve <abs path>`. Fails on unchanged `main`: `servers.update(overlay_servers)` (`manifest/loader.py:801`) applies the replacement, and the only warning emitted (`:806-809`) names no command.
- [ ] EC-CONSENT-2 — proven by `uv run pytest -q tests/test_project_source_consent_config.py::test_unapproved_project_mcp_json_is_not_applied_by_load_configs tests/test_project_source_consent_config.py::test_unapproved_project_mcp_json_is_not_applied_by_load_config_sources tests/test_project_source_consent_config.py::test_unapproved_project_mcp_json_is_not_applied_by_load_disabled_auto_start tests/test_project_source_consent_config.py::test_unapproved_project_mcp_json_is_not_applied_by_load_enabled_auto_start tests/test_project_source_consent_config.py::test_unapproved_project_mcp_json_does_not_set_allow_private_registry tests/test_project_source_consent_config.py::test_unapproved_project_mcp_json_skip_logs_the_trust_approve_command`, falsified by placing an unapproved project `.mcp.json` declaring a server plus `disableAutoStart`, `autoStart` and `allowPrivateRegistry`, and asserting each of the four readers returns as if the file were absent. Fails on unchanged `main`: all four read sites (`config/loader.py:281-283`, `:796-813`, `:881-883`, `:925-927`) honour it today.
- [ ] EC-CONSENT-3 — proven by `uv run pytest -q tests/test_project_source_consent_policy.py::test_project_policy_cannot_allow_what_user_policy_denies tests/test_project_source_consent_policy.py::test_project_policy_cannot_widen_a_user_allowlist tests/test_project_source_consent_policy.py::test_project_policy_denial_still_applies_on_top_of_user_policy tests/test_project_source_consent_policy.py::test_project_policy_limits_take_the_minimum_of_the_user_effective_and_project_explicit_values tests/test_project_source_consent_policy.py::test_project_policy_unset_limits_do_not_lower_a_raised_user_limit tests/test_project_source_consent_policy.py::test_a_project_limit_cannot_raise_a_limit_the_user_never_set tests/test_project_source_consent_policy.py::test_policy_digest_covers_the_project_policy`, falsified by a user `~/.claude/gateway-policy.yaml` with `servers.denylist: ["evil"]` and an **approved** project `.mcp-gateway-policy.yaml` with `servers.allowlist: ["evil"]`, asserting `is_server_allowed("evil") is False`. Fails on unchanged `main`: `_default_policy_paths()` returns the project file first and `__init__` breaks after loading it (`policy/policy.py:71-75`), so the user file is never read and `evil` is allowed.
- [ ] EC-CONSENT-4 — proven by `uv run pytest -q tests/test_project_consent_gate.py::test_content_change_after_approval_is_not_approved tests/test_project_source_consent_manifest.py::test_editing_an_approved_overlay_revokes_it tests/test_project_source_consent_config.py::test_editing_an_approved_project_mcp_json_revokes_it tests/test_project_source_consent_policy.py::test_editing_an_approved_project_policy_revokes_it`, falsified at each of the three production sites by approving the file's current bytes, asserting it applies, appending one byte, and asserting it no longer applies and the refusal is logged. Fails on unchanged `main`: no site consults any store, so the edited file applies unchanged.
- [ ] EC-CONSENT-5 — proven by `uv run pytest -q tests/test_policy.py tests/test_manifest.py tests/test_manifest_provision.py` together with `git diff --name-only origin/main..HEAD -- tests/test_policy.py tests/test_manifest.py tests/test_manifest_provision.py` returning **empty** (these three create no project-scoped fixture and must pass *unmodified*), and by `uv run pytest -q tests/test_project_source_consent_manifest.py::test_user_scope_overlay_is_applied_without_any_trust_record tests/test_project_source_consent_manifest.py::test_env_scope_manifest_path_is_applied_without_any_trust_record tests/test_project_source_consent_config.py::test_user_mcp_json_is_applied_without_any_trust_record tests/test_project_source_consent_config.py::test_custom_config_path_is_applied_without_any_trust_record tests/test_project_source_consent_policy.py::test_explicit_policy_path_skips_discovery_and_composition tests/test_project_source_consent_policy.py::test_user_policy_alone_is_unchanged`. **The roadmap's own proof clause for this criterion is defective and is superseded here** — see the EC-CONSENT-5 defect in Execution Notes: `tests/test_config_loader.py`, `tests/test_manifest_overlay.py` and `tests/test_policy_fail_open.py` cannot pass unmodified, because they assert that *unapproved* project sources apply. This is also the phase's only non-regression criterion: it passes on unchanged `main` by construction, and its falsifier is a mutation falsifier — the `*_without_any_trust_record` tests fail against any implementation that gates user, env, custom or `--policy` scope.
- [ ] EC-CONSENT-6 — proven by `uv run pytest -q tests/test_project_source_consent_manifest.py::test_unapproved_overlay_cannot_add_a_new_server tests/test_project_source_consent_manifest.py::test_an_added_server_from_an_unapproved_overlay_is_not_manifest_backed tests/test_project_source_consent_manifest.py::test_unapproved_overlay_server_env_patch_is_not_applied`, falsified by an unapproved `.pmcp/manifest.yaml` declaring a server name absent from the shipped manifest and asserting `load_manifest().get_server("<added>") is None` — the exact predicate `tools/handlers.py:4279` uses to decide manifest-backed, so PKGID's default-deny exemption cannot fire on it. Fails on unchanged `main`: `servers.update(overlay_servers)` inserts unknown keys, so `get_server` returns the added entry today.
- [ ] EC-CONSENT-7 — proven by `uv run pytest -q tests/test_project_source_consent_policy.py::test_unapproved_project_policy_is_not_read tests/test_project_source_consent_policy.py::test_unapproved_project_policy_does_not_shadow_the_user_policy tests/test_project_source_consent_policy.py::test_unapproved_project_policy_refusal_names_the_trust_approve_command`, falsified by an unapproved project `.mcp-gateway-policy.yaml` that is allow-all beside a user policy denying `evil`, asserting `is_server_allowed("evil") is False`, that `policy_digest` equals the user-policy-only digest (the project file contributed nothing, not merely nothing extra), and that the WARNING names `pmcp trust approve <abs path>`. Fails on unchanged `main`: the project file is loaded and `break`s the discovery loop, so `evil` is allowed and the digest is the project policy's.

## Verification

```bash
uv run pytest -q tests/test_project_consent_gate.py \
  tests/test_project_source_consent_manifest.py \
  tests/test_project_source_consent_config.py \
  tests/test_project_source_consent_policy.py
uv run pytest -q tests/test_config_loader.py tests/test_policy.py tests/test_policy_fail_open.py \
  tests/test_manifest.py tests/test_manifest_overlay.py tests/test_manifest_provision.py
uv run pytest -q tests/test_trust_store.py tests/test_trust_cli.py   # TRUST's suite must stay green
uv run pytest -q tests/                 # compare counts to the pre-phase baseline, same dir
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
uv run python scripts/check_workflows.py --base-ref origin/main
uv run python -c "import pmcp.project_consent"          # gate module importable

# The project-fixture-free suites must pass UNMODIFIED (EC-CONSENT-5) — this must be empty.
# test_config_loader.py / test_manifest_overlay.py / test_policy_fail_open.py are
# DELIBERATELY absent: they assert unapproved project sources apply, and are updated
# by their owning lanes. See the EC-CONSENT-5 roadmap defect in Execution Notes.
git diff --name-only origin/main..HEAD -- tests/test_policy.py tests/test_manifest.py \
  tests/test_manifest_provision.py

# CONSENT must not leak into PKGID's or EGRESS's surfaces — this must be empty:
git diff --name-only origin/main..HEAD -- src/pmcp/tools/handlers.py src/pmcp/manifest/installer.py \
  src/pmcp/validation.py src/pmcp/cli.py src/pmcp/trust_store.py
```

Host note: `/tmp/package.json` makes ~107 npm-identity tests fail on some hosts
(`tests/conftest.py`); compare the same command from the same directory before
and after, never against a clean-machine expectation.

## Spec Closeout Plan

- schema: `spec_delta_closeout.v1`
- decision: `roadmap_amendment`
- target surfaces: `src/pmcp/project_consent.py`, `src/pmcp/manifest/loader.py`, `src/pmcp/config/loader.py`, `src/pmcp/policy/policy.py`
- evidence paths: `tests/test_project_consent_gate.py`, `tests/test_project_source_consent_manifest.py`, `tests/test_project_source_consent_config.py`, `tests/test_project_source_consent_policy.py`, `CHANGELOG.md`
- redaction posture: `metadata_only`
- downstream handling: roadmap amendment — the CONSENT section's single evidence path `tests/test_project_source_consent.py` is superseded by the four files above, and the "3 lanes" scope note by four impl lanes plus the terminal docs lane. SECURITY.md remains SEAL's, unwritten here by design.

## Execution Policy

- default: effort=medium, reason=three existing loaders whose current behaviour is load-bearing for working setups
- SL-1: effort=high, reason=a security predicate whose wrong default silently grants trust to repository-supplied bytes
- SL-4: effort=high, reason=policy composition semantics where a widening bug reopens S-11 invisibly and Consiliency/pmcp#202 fail-closed must survive
- SL-5: effort=low, reason=catalog rescan and changelog only
