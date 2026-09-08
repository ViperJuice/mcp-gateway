# PMCP phase plans v13 — trust boundaries

> **Revision 2 (2026-09-08).** Boarded 3x DISAGREE. Seven real defects, fixed below.
> The load-bearing ones: the DAG claimed CONSENT and PKGID "share no files" while both
> list `policy/policy.py`; an unapproved project overlay that *adds* a server (rather
> than replacing one) slipped the seam between CONSENT and PKGID; and IF-0-TRUST-1
> froze the record shape but not **where the store lives or who may write it** — a
> checkout-writable store lets a repository ship its own approval and makes
> absence-is-not-assent meaningless.
>
> **One verdict is rejected.** Two seats judged this as a pre-merge implementation
> review — "no implementation evidence, exit criteria unchecked, missing evidence is
> `contract_bug`". A roadmap is a plan; unchecked exit criteria are its purpose, and the
> `spec_delta_closeout` evidence rule binds *phase execution*, not the roadmap artifact.
> Their structural findings are folded in; the evidence objection is not.


## Context

PMCP is a local-first MCP gateway that brokers untrusted third-party servers on
behalf of a semi-trusted, prompt-injectable agent. The 2026-09-01 codebase review
(`plans/codebase-review-2026-09-01.md`) found that the remaining risk has moved up
a level: not bugs inside functions, but the **trust boundaries between the agent,
repository-supplied configuration, and the host**.

Four findings look separate and are one question — *what may introduce code the
gateway will execute or actions it will take, and who consents?*

| Finding | Severity | Substance |
|---|---|---|
| **S-01** | HIGH | `register_discovered_server` accepts any npm package; `provision` runs it with `npx -y`. Policy checks the **agent-chosen server name**, not the package. Reproduced: allowlisting `internal-approved-tool` executes `npx -y totally-arbitrary-evil-package`. |
| **S-03** | MEDIUM (HIGH shared/CI) | A checkout's `.pmcp/manifest.yaml` replaces any shipped server's command wholesale; `.mcp.json` is trusted the same way. "Clone a repo and use GitHub" becomes code execution. |
| **S-11** | MEDIUM | A project `.mcp-gateway-policy.yaml` **silently shadows** the operator's global policy — first match wins, project paths first, default allow-all. |
| **S-04** | MEDIUM | `gateway.submit_feedback` posts agent-authored text to a public repo using the ambient `GITHUB_TOKEN`. |

Each has a local patch. Patching them separately would produce four inconsistent
consent mechanisms, and S-11's own fix note already says it "ties into S-03's trust
store". This roadmap builds **one** trust model and routes all four through it.

## Architecture North Star

```
        agent (prompt-injectable)        repository checkout        operator
              |                                 |                      |
              | register / provision            | .pmcp/manifest.yaml  | pmcp trust
              | submit_feedback                 | .mcp.json            | pmcp approve
              v                                 v  policy.yaml         v
   +---------------------------------------------------------------------+
   |  TRUST  identity + provenance + approval record (the frozen core)    |
   |    package identity: (registry, name, resolved version, integrity)   |
   |    source provenance: (abs path, content hash, scope, decision)      |
   +---------------------------------------------------------------------+
        |                        |                            |
        v                        v                            v
   PKGID: provisioning     CONSENT: project-scoped      EGRESS: outbound acts
   binds to package        config gated + policy        with operator identity
   identity, not a name    narrowing-only               explicit + preview-first
        \                        |                            /
         \                       v                           /
          +--------------> SEAL: documented model + ---------+
                           adversarial end-to-end proof
```

## Assumptions (fail-loud if wrong)

1. Downstream tool output is untrusted and the agent is prompt-injectable. If the
   agent is trusted, most of this roadmap is unnecessary.
2. The operator is a human who can be asked once and remembered — an approval
   store is meaningful. If PMCP runs fully unattended, the default must be deny,
   not prompt.
3. `provision` consults the shipped manifest **before** the discovered registry
   (`handlers.py:4262-4266`), so a discovered entry cannot shadow a manifest name.
   Verified in the review; if it regresses, S-01's blast radius grows.
4. The npm identity machinery from #195 (`manifest/npm_resolver.py`,
   `manifest/version_checker.py`) can resolve a package identity without executing
   it. TRUST extends that work rather than starting a new subsystem.
5. Existing operators keep working configurations. A default-deny that silently
   breaks a working setup is a failed phase, not a strict one.

## Non-Goals

- Sandboxing downstream servers, or any container/namespace isolation.
- Sanitising shell-exported secrets — deliberate, documented in SECURITY.md, and
  unchanged by this roadmap (#229 closed the plain-`.env` path only).
- Auditing PMCP's own supply chain (that is #217/#228 territory).
- A general policy engine. Policy gains package identifiers and narrowing
  semantics; it does not become a rules language.
- Retroactive approval of already-provisioned servers beyond a one-time migration.

## Cross-Cutting Principles

- **Fail closed on the new gates, fail open on nothing.** Every gate added here
  refuses on ambiguity, matching #202 and #217.
- **Identity, not labels.** A decision binds to what will execute (package,
  file content hash), never to a name the agent supplied.
- **Consent is recorded, revocable, and legible.** An approval is a durable record
  an operator can list and revoke — not a flag buried in a config.
- **A narrowing-only rule for project scope.** Repository-supplied configuration
  may restrict, never widen.
- **Every refusal names the decision and how to grant it.** A blocked action must
  print the exact `pmcp` command that would allow it.
- **No behaviour change without a CHANGELOG entry**, and no security claim in
  SECURITY.md that the tests do not prove.

## Phases

### Phase 1 — Trust primitives (TRUST)

**Objective**
Freeze the two data contracts every other phase codes against: a package identity
tuple and a source-provenance/approval record, with a store to read and write them.

**Exit criteria**
- [ ] EC-TRUST-1 — a package identity `(registry, name, resolved_version, integrity)` is resolvable for any npm spec without executing it, reusing `manifest/npm_resolver.py`; proven by a test that resolves a real spec offline from a fixture.
- [ ] EC-TRUST-2 — a source provenance record `(absolute_path, content_sha256, scope, decision, recorded_at)` round-trips through the store; a file whose content changes after approval reads back as **not approved**.
- [ ] EC-TRUST-3 — the store refuses to answer "approved" for any path it has no record of; absence is never assent.
- [ ] EC-TRUST-4 — the full CLI surface exists and is covered by tests: `pmcp trust approve <path>` (records), `pmcp trust list`, `pmcp trust revoke <path>`. The **approve** verb is what every downstream refusal message names, so omitting it would leave those messages naming a command that does not exist.
- [ ] EC-TRUST-5 — the store lives **outside any repository** (user scope, e.g. `~/.config/pmcp/`), and a store path inside the current checkout is refused at startup. A repository that ships its own approval record must not be believed; without this, absence-is-not-assent is decorative.
- [ ] EC-TRUST-6 — `is_approved` answers about **bytes, not paths**: the caller passes the content it is about to apply and the store hashes that, so a file swapped between check and use is not approved by a stale decision.

**Scope notes**
Decompose into 2 lanes with disjoint files: **lane A** owns the provenance/approval
store and its CLI surface; **lane B** owns the package-identity resolver adapter over
the existing #195 machinery. Both publish their shapes on day 1 so CONSENT and PKGID
can start against the contract rather than the implementation. No caller is wired in
this phase — that is deliberate, and it is why this phase is small.

**Non-goals**
Wiring any consumer; changing provisioning or policy behaviour.

**Key files**
- `src/pmcp/trust_store.py` (new)
- `src/pmcp/manifest/npm_resolver.py`
- `src/pmcp/cli.py`
- `tests/test_trust_store.py` (new)

**Depends on**
- (none)

**Produces**
- IF-0-TRUST-1
- IF-0-TRUST-2

**Spec closeout policy**
- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `src/pmcp/trust_store.py`, `src/pmcp/cli.py`
- evidence paths: `tests/test_trust_store.py`, `CHANGELOG.md`
- redaction posture: `metadata_only`
- missing or malformed evidence routes to `blocker_class=contract_bug` (non-human).

### Phase 2 — Project-scoped configuration requires consent (CONSENT)

**Objective**
Gate every repository-supplied configuration source through the trust store, and
make a project policy able only to narrow the operator's policy.

**Exit criteria**
- [ ] EC-CONSENT-1 — an unapproved `.pmcp/manifest.yaml` does **not** replace a shipped server's command; the shipped definition is used and the skip is logged at WARNING naming the exact `pmcp trust` command.
- [ ] EC-CONSENT-2 — an unapproved project `.mcp.json` is not applied, on the same terms.
- [ ] EC-CONSENT-3 — a project policy may only **narrow**: a project file that allows a server the user policy denies leaves it denied; proven by a test that fails on today's first-match-wins behaviour.
- [ ] EC-CONSENT-4 — approving a file, then editing it, revokes the approval automatically (content hash, not path).
- [ ] EC-CONSENT-5 — an operator with **no project files** sees byte-identical behaviour to today, proven by the suites that contain no project-scoped case passing unmodified. **WAS WRONG (rev 2):** it said "the existing config/policy suites passing unmodified", which is unsatisfiable — `tests/test_manifest_overlay.py::test_project_overrides_user_overrides_shipped` asserts that an *unapproved* overlay replaces a shipped command, `tests/test_config_loader.py` carries many project `.mcp.json` cases, and `tests/test_policy_fail_open.py` is entirely about discovered project policy. Those three suites encode the behaviour this phase changes and are updated by their owning lanes; demanding they pass unmodified would have forced an implementer to weaken the gate until the old assertions held.
- [ ] EC-CONSENT-6 — an unapproved overlay that **adds a new server** rather than replacing a shipped one is also not applied, and the added server is **not** treated as manifest-backed downstream. Without this the add path slips the seam: CONSENT only refuses replacement, PKGID's default-deny exempts manifest-backed servers, and an unapproved package executes through the gap between two phases that each look complete.
- [ ] EC-CONSENT-7 — an unapproved project `.mcp-gateway-policy.yaml` is **not applied at all** (not merely narrowed), on the same trust terms as the manifest and `.mcp.json`. EC-CONSENT-3 governs what an *approved* project policy may do; this governs whether it is read.

**Scope notes**
Decompose into 3 lanes with disjoint files: **lane A** owns the manifest overlay
walk-up (`manifest/loader.py`), **lane B** owns `.mcp.json` (`config/loader.py`),
**lane C** owns policy precedence and intersection (`policy/policy.py`). The three
consume IF-0-TRUST-1 and never write to each other's files. Lane C is the one with a
behaviour change an existing operator could feel — sequence its CHANGELOG note first.

**Non-goals**
Package identity (PKGID owns it); any change to user- or env-scoped sources.

**Key files**
- `src/pmcp/manifest/loader.py`
- `src/pmcp/config/loader.py`
- `src/pmcp/policy/policy.py`
- `tests/test_project_source_consent.py` (new)

**Depends on**
- TRUST

**Produces**
- IF-0-CONSENT-1

**Spec closeout policy**
- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `src/pmcp/manifest/loader.py`, `src/pmcp/config/loader.py`, `src/pmcp/policy/policy.py`
- evidence paths: `tests/test_project_source_consent.py`, `CHANGELOG.md`
- redaction posture: `metadata_only`
- missing or malformed evidence routes to `blocker_class=contract_bug` (non-human).

### Phase 3 — Provisioning binds to package identity (PKGID)

**Objective**
Stop the agent choosing what executes. Bind provisioning decisions to a resolved
package identity and put discovered-package installation behind an operator opt-in.

**Exit criteria**
- [ ] EC-PKGID-1 — the review's reproduction fails closed: registering `internal-approved-tool` with package `totally-arbitrary-evil-package` and provisioning it does **not** exec `npx -y totally-arbitrary-evil-package`; the refusal names the package and the command that would approve it.
- [ ] EC-PKGID-2 — policy can allow or deny **package identifiers**, and `provision` checks the package, not only the server name.
- [ ] EC-PKGID-3 — discovered-package provisioning is default-deny; an operator opt-in (config flag or recorded approval) is required, and a manifest-backed server is unaffected.
- [ ] EC-PKGID-4 — the exact argv is logged at WARNING before every install spawn.
- [ ] EC-PKGID-5 — a registration without a resolvable version is refused, or the version is resolved and recorded at registration and passed as `pkg@<version>`; whichever is chosen is asserted by test.
- [ ] EC-PKGID-6 — servers already provisioned before this phase keep working: a one-time migration records approvals for them, or they are grandfathered by an explicit recorded decision. Assumption 5 says a default-deny that silently breaks a working setup is a failed phase; this criterion is the phase that owns it, and no other phase did.

**Scope notes**
Decompose into 3 lanes with disjoint files: **lane A** owns the register/provision
gate in `tools/handlers.py`, **lane B** owns policy package identifiers in
`policy/policy.py` and `types.py`, **lane C** owns argv logging plus version pinning
in `manifest/installer.py`. Lane A is the single-writer risk — `handlers.py` is large
and touched by other work; serialise any other writer against it. Parallel-safe with
CONSENT: no shared file, and both depend only on TRUST.

**Non-goals**
Sandboxing what does run; auditing PMCP's own dependencies.

**Key files**
- `src/pmcp/tools/handlers.py`
- `src/pmcp/policy/policy.py`
- `src/pmcp/manifest/installer.py`
- `src/pmcp/validation.py`
- `tests/test_package_identity_gate.py` (new)

**Depends on**
- TRUST

**Produces**
- IF-0-PKGID-1

**Spec closeout policy**
- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `src/pmcp/tools/handlers.py`, `src/pmcp/policy/policy.py`, `src/pmcp/manifest/installer.py`
- evidence paths: `tests/test_package_identity_gate.py`, `CHANGELOG.md`
- redaction posture: `metadata_only`
- missing or malformed evidence routes to `blocker_class=contract_bug` (non-human).

### Phase 4 — Outbound actions need explicit authority (EGRESS)

**Objective**
Stop `gateway.submit_feedback` acting publicly under the operator's ambient identity;
make preview the default and explicit opt-in the only path to submission.

**Exit criteria**
- [ ] EC-EGRESS-1 — ambient `GITHUB_TOKEN` is never used; only a dedicated `PMCP_FEEDBACK_TOKEN` is honoured, asserted by a test that sets `GITHUB_TOKEN` and requires no request to be attempted.
- [ ] EC-EGRESS-2 — the default is preview-only: the handler returns the payload and a browser URL, and `confirm_submission` alone cannot cause a post without `enable_feedback_submission: true`.
- [ ] EC-EGRESS-3 — the default repository is corrected to the real remote, asserted against the value in the packaged config rather than a literal duplicated in the test.
- [ ] EC-EGRESS-4 — no blocking HTTP call runs on the event loop in this path (addresses P-03).

**Scope notes**
Decompose into 2 lanes with disjoint concerns in one file: **lane A** owns the
credential and consent gate, **lane B** owns the repository default and moving HTTP
off the loop. Both touch `tools/handlers.py:4796-4992` — treat that range as a
single-writer region and serialise the two lanes if the phase is executed
concurrently. Root phase: it shares the consent *principle* with TRUST but none of
its data contracts, so forcing a dependency would serialise the roadmap for nothing.

**Non-goals**
Redesigning the feedback feature; the wider redaction rework (that is S-12/#234).

**Key files**
- `src/pmcp/tools/handlers.py`
- `tests/test_feedback_egress.py` (new)

**Depends on**
- (none)

**Produces**
- IF-0-EGRESS-1

**Spec closeout policy**
- schema: `spec_delta_closeout.v1`
- decision: `no_spec_delta`
- target surfaces: `src/pmcp/tools/handlers.py`
- evidence paths: `tests/test_feedback_egress.py`, `CHANGELOG.md`
- redaction posture: `metadata_only`
- missing or malformed evidence routes to `blocker_class=contract_bug` (non-human).

### Phase 5 — Document and prove the model (SEAL)

**Objective**
State the trust model in SECURITY.md exactly as implemented, and prove the four
boundaries hold together against an adversarial end-to-end suite.

**Exit criteria**
- [ ] EC-SEAL-1 — SECURITY.md describes the implemented model with no claim the tests do not prove; each claim cites the test that proves it.
- [ ] EC-SEAL-2 — an adversarial suite drives the four review reproductions end to end and each fails closed, run against the real handlers rather than mocks.
- [ ] EC-SEAL-5 — the **composition** cases fail closed too, not only the four original reproductions: an unapproved overlay that adds a server (EC-CONSENT-6), and an approval record shipped inside the checkout (EC-TRUST-5). Both are seams between phases that each pass their own criteria, which is exactly what a per-phase suite cannot catch.
- [ ] EC-SEAL-3 — every refusal path prints the exact `pmcp` command that would grant the action, asserted for each gate.
- [ ] EC-SEAL-4 — a fresh operator with no trust store and no project files sees unchanged behaviour for manifest-backed servers.

**Scope notes**
Decompose into 2 lanes with disjoint files: **lane A** owns SECURITY.md and the
CHANGELOG narrative, **lane B** owns the adversarial end-to-end suite. Lane B is the
one that can fail late, so start it as soon as PKGID and CONSENT publish their gates
rather than waiting for this phase to open.

**Non-goals**
New gates. This phase proves and documents; it does not add behaviour.

**Key files**
- `SECURITY.md`
- `CHANGELOG.md`
- `tests/test_trust_boundaries_e2e.py` (new)

**Depends on**
- CONSENT
- PKGID
- EGRESS

**Produces**
- IF-0-SEAL-1

**Spec closeout policy**
- schema: `spec_delta_closeout.v1`
- decision: `canonical_spec_update`
- target surfaces: `SECURITY.md`
- evidence paths: `tests/test_trust_boundaries_e2e.py`, `CHANGELOG.md`
- redaction posture: `metadata_only`
- missing or malformed evidence routes to `blocker_class=contract_bug` (non-human).

## Top Interface-Freeze Gates

- IF-0-TRUST-1 — the provenance/approval record `(absolute_path, content_sha256, scope, decision, recorded_at)`; `is_approved(path, content: bytes) -> bool` (**hashes the bytes the caller is about to apply**, closing the check-then-use window), `record(path, content, scope, decision)`, `revoke(path)`. The freeze also fixes **store residency and write authority**: user-scoped, outside any repository, never agent-writable. Frozen before CONSENT and PKGID start — a downstream lane that has to guess residency cannot start.
- IF-0-TRUST-2 — the package identity tuple `(registry, name, resolved_version, integrity)` and the resolver that produces it without executing the package.
- IF-0-CONSENT-1 — the project-source gate decision surface: given a candidate source path and scope, the single call every loader uses to decide whether to apply it.
- IF-0-PKGID-1 — the provisioning gate: given a server config and a resolved package identity, the single call `provision` uses to decide whether an install may spawn.
- IF-0-EGRESS-1 — the outbound-action gate: the credential and consent predicate `submit_feedback` consults before any network call, and the preview payload shape returned when it refuses.
- IF-0-SEAL-1 — the documented trust model in SECURITY.md, each claim bound to a proving test.

## Phase Dependency DAG

```
  TRUST ─┬─> CONSENT ─┐
         │            ├─> SEAL
         └─> PKGID ───┤
                      │
  EGRESS ─────────────┘   (root; parallel with TRUST from day 1)
```

- `CONSENT` and `PKGID` both depend only on `TRUST` and can be **planned** concurrently, but they are **not file-disjoint**: both write `src/pmcp/policy/policy.py` (CONSENT lane C, PKGID lane B). Execute those two lanes serially against that file, or land one phase's policy lane before opening the other's. **WAS WRONG (rev 1):** this line claimed they "share no files", contradicting both phases' own Key files.
- `EGRESS` shares no ancestor with `TRUST` — it can start immediately, in parallel with everything.
- Critical path: `TRUST → PKGID → SEAL` (PKGID is the largest phase).

## Execution Notes

- Plan in DAG order: `/claude-plan-phase TRUST` first. Once TRUST merges, run
  `/claude-plan-phase CONSENT` and `/claude-plan-phase PKGID` **concurrently**.
  `/claude-plan-phase EGRESS` can be planned at any time, including now.
- Execute with `/claude-execute-phase <alias>` per phase. `CONSENT` and `PKGID` are
  parallel-safe; `EGRESS` is parallel with all of them.
- `SEAL` opens only after `CONSENT`, `PKGID` and `EGRESS` have merged, but its lane B
  (adversarial suite) should be written against the gates as they land.
- Single-writer hazards to serialise: `src/pmcp/tools/handlers.py` is written by
  PKGID lane A and both EGRESS lanes; `src/pmcp/policy/policy.py` by CONSENT lane C
  and PKGID lane B. Do not run those lanes against the same file concurrently.
- **CONSENT and PKGID may be planned concurrently, but must execute serially.**
  They are sibling branches off TRUST with no freeze between them, so the DAG
  permits parallel execution — but their lane plans declare four shared writers:
  `src/pmcp/policy/policy.py` (CONSENT SL-4 and PKGID SL-2 both own it),
  plus `.claude/docs-catalog.json`, `CHANGELOG.md`, and this roadmap file from
  the two docs lanes. Each plan is internally disjoint; the collision is only
  visible pairwise across the two phases, which no single plan's own
  ownership check can see. Execute one, merge it, then execute the other.
- Every phase ships a CHANGELOG entry; `SECURITY.md` is written once, in SEAL, to
  avoid four partial descriptions of one model.

## Verification

```bash
# Each phase's own suite
uv run pytest -q tests/test_trust_store.py
uv run pytest -q tests/test_project_source_consent.py
uv run pytest -q tests/test_package_identity_gate.py
uv run pytest -q tests/test_feedback_egress.py

# The roadmap's end-to-end proof: the four review reproductions must fail closed
uv run pytest -q tests/test_trust_boundaries_e2e.py

# No regression for an operator with no trust store and no project files
uv run pytest -q tests/                 # compare counts to the pre-roadmap baseline
uv run ruff check src/ tests/ scripts/ && uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
uv run python scripts/check_workflows.py --base-ref origin/main
```

Host note: `/tmp/package.json` makes ~107 npm-identity tests fail on some hosts
(`tests/conftest.py`); compare the same command from the same directory before and
after, never against a clean-machine expectation.
