# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **The operator's project `.env` no longer reaches the servers PMCP spawns.**
  `pmcp`'s entry point loads `<project>/.env` into its own environment at
  startup, and `_check_api_key_available` loads whole env files to answer a
  boolean — while `sanitized_subprocess_env` builds every downstream server's
  environment from `os.environ.copy()`. Every key in the operator's `.env`, not
  just PMCP's own, was therefore inherited by every third-party MCP server the
  gateway launched. Both load sites now record the keys their `load_dotenv` call
  introduced into the process, and the subprocess sanitizer strips exactly those.
  The mechanism is recorded provenance, never a re-parse: `load_dotenv`'s
  semantics are untouched — interpolation, `override=False` precedence and the
  credential-availability boolean are all bit-for-bit unchanged — and a variable
  the operator exported into their shell that merely shares a name with a `.env`
  entry is not in the delta, so it is not stripped (stripping by name would have
  deleted the operator's `PATH` from every spawned server). A server's own
  declared `env_var` may still come from a plain `.env`: `own_env` is applied
  after the strip, so that key reaches that server and nothing else in the file
  does. Shell-exported secrets are still inherited, deliberately. Found by the
  2026-09-01 codebase review (S-02); see
  [#229](https://github.com/Consiliency/pmcp/issues/229).
- **Dependency advisories on the auth path are closed, and CI now fails on new
  ones.** `pip-audit` reported nine advisories against the shipped lockfile; the
  one that mattered most was **PYSEC-2026-176 in PyJWT 2.10.1, a verifier-side
  algorithm allow-list bypass in `jwt.decode()`** — the exact call in
  `src/pmcp/auth.py`, and the exact control SECURITY.md promises ("signatures are
  only accepted for the operator-configured algorithm allowlist; the token's own
  `alg` header is never trusted"). Also closed: PYSEC-2026-175 (`PyJWKClient`
  passing its URI straight to `urllib.request.urlopen`) and PYSEC-2026-177 (an
  unknown-`kid` token forcing a JWKS refetch), both adjacent to the auth work in
  [#210](https://github.com/Consiliency/pmcp/issues/210) /
  [#211](https://github.com/Consiliency/pmcp/issues/211); advisories in
  `cryptography` (which performs the signature verification) and `aiohttp` (the
  JWKS fetch client); plus `starlette`, `python-multipart`, `python-dotenv`,
  `click` and `pytest`. `aiohttp`'s declared floor is raised to `>=3.14.2` and
  PyJWT's to `>=2.13.0`, because a floor that admits a vulnerable range is not a
  pin. A new blocking `audit` CI job runs `pip-audit --strict` against the
  resolved environment, so the next advisory is a red X rather than something a
  manual review finds months later. Found by the 2026-09-01 codebase review
  (D-01); see [#224](https://github.com/Consiliency/pmcp/issues/224).


### Fixed
- **Downstream failures no longer log `unhandled errors in a TaskGroup` and
  nothing else.** Every remote-transport path in `ClientManager` runs inside an
  anyio task group, and `str(ExceptionGroup)` names neither the type nor the
  message of what actually failed — so twenty log sites reported only that
  string for any failure. A week of CI hangs
  ([#200](https://github.com/Consiliency/pmcp/issues/200)) produced exactly it,
  which is why the cause stayed unknown. `describe_exception()` flattens a
  group to its leaf exceptions (`ConnectionResetError: peer went away`), and is
  used at every site that logs a caught exception, including
  `disconnect_server`'s returned error string. Detection is by duck-typing
  `.exceptions`, because 3.11+ raises the builtin `BaseExceptionGroup` while
  3.10 raises `exceptiongroup.ExceptionGroup` from the backport. The rendered
  text goes through `sanitize_auth_diagnostic`, so flattening cannot widen
  secret exposure — most of these sites logged the raw exception before and are
  redacted now. `last_error` (surfaced by `pmcp status`, `pmcp doctor` and
  health output) and the error strings `connect_server` and `disconnect_server`
  return to their callers were rendering the group string too, and are fixed as
  well. An AST guard fails CI if a caught exception is interpolated into an
  f-string or passed to `str()` anywhere inside its handler — not merely inside
  a `logger` call, which was the guard's first, too-narrow form. The one
  `exc_info=` call site is gone too: `exc_info` hands the raw exception to the
  logging machinery, which appends the unredacted exception tree *after* the
  sanitized message, so a bearer token in a transport error reached the log in
  full. The traceback is now formatted in-process and sanitized, keeping the
  frames. See
  [#224](https://github.com/Consiliency/pmcp/issues/224).


### Changed
- **Every GitHub Action is pinned to a commit SHA.** All 30 remote `uses:`
  references — 29 across the five workflows and the one inside the local
  composite action `.github/actions/pipeline-bootstrap-setup` — now read
  `owner/action@<40-hex-sha> # vX.Y.Z`. Every pin is the commit its previous
  mutable ref resolved to on the day, so **no action runs a different version
  after this change**: the PyPI publish action is pinned to v1.14.2, the commit
  `release/v1` already pointed at, and the composite's `setup-node@v4` to
  v4.4.0, the commit `v4` pointed at. `scripts/check_workflows.py` gains a
  raw-text invariant that fails CI on any remote `uses:` not in that form —
  in workflows and in local actions, `.yml` and `.yaml` alike, and cross-checks
  that inventory against the parsed document so a `uses:` spelled in a form the
  line scan cannot see (`"uses":`, `{uses: …}`, a block scalar) fails rather than
  runs unpinned — and its exact
  allowlist for `release.yml` now names commits, so a form-valid pin to the
  wrong commit (a moved digit, or a real older release with an honest comment)
  is rejected too. Dependabot gains an entry for the composite action's
  directory, which its root entry had never scanned. Six new mutants prove
  each of those catches (`.consiliency/evidence/mutation-217.md`); see
  [#217](https://github.com/Consiliency/pmcp/issues/217).
- CI: `actions/setup-node` v4 → v7 in the workflows (Dependabot, #216).
- CI: pinned actions moved by Dependabot — the composite's `actions/setup-node`
  v4.4.0 → v7.0.0 (#219), `actions/download-artifact` v7.0.0 → v8.0.1 on the
  release path (#221; hash mismatches on download now error instead of warn),
  `astral-sh/setup-uv` v7.6.0 → v10.0.1 (#220). Each release-path bump carries
  its `EXPECTED_USES` update in the same PR.

## [2.7.3] - 2026-08-31

### Changed
- **PMCP no longer implies it verified where a server-supplied auth URL
  points.** It never did: `_is_public_auth_host` classifies IP literals and
  accepts a DNS name **without resolving it**, so
  `https://metadata.google.internal/...` in an elicitation payload or a
  `WWW-Authenticate` header was relayed to the operator — and to an agent —
  looking like something PMCP had checked. Names are still relayed, because
  refusing unresolvable ones would refuse a well-behaved server's
  `https://auth.vendor.com/...` too; what changed is what PMCP claims about
  them. `UrlElicitationInfo.url_verified`, `AuthMetadataInfo.verified_urls`
  (per URL field, since those five URLs are independent) and
  `AuthChallengeInfo.resource_metadata_url_verified` report whether PMCP itself
  classified the host as a public literal, and all three default to
  **unverified**. The qualification is threaded into the `next_step` string an
  agent actually follows, and into `pmcp auth connect` / `pmcp auth acknowledge`
  output in both human text and `--json`. `pmcp doctor` no longer reports a bare
  `[OK] ... configured at <name>` for a host it did not verify
  ([#211](https://github.com/Consiliency/pmcp/issues/211)).
- **A downstream server can no longer hand back a loopback `http://` URL.**
  `sanitize_url_elicitation_url` now splits by provenance: a URL parsed out of a
  server's error payload loses loopback HTTP, while one the operator types into
  `gateway.auth_connect` keeps it, because local OAuth redirects to
  `http://127.0.0.1`. The default is the strict remote policy, so a call site
  missed by a future change fails closed (#211).
- **`fetch_json_metadata` now fails closed.** PMCP retrieves that URL itself, so
  "accepted but unresolved" is not good enough there: the host must be one PMCP
  verified as a public IP literal, and a refused URL is rejected before the
  opener is reached rather than fetched and judged afterwards. `http://127.0.0.1/x`
  and unresolved names previously both reached `urlopen` (#211).

## [2.7.2] - 2026-08-30

### Fixed
- **The public auth-URL host check accepted several non-public hosts.** Two
  distinct defects in `_is_public_auth_host`, both reachable through auth
  metadata, elicitation, and `resource_server_jwks_url`:
  - The classifier subtracted a list of bad properties (`is_private`,
    `is_loopback`, `is_link_local`, `is_multicast`, `is_unspecified`), and that
    list had holes. RFC 6598 CGNAT (`100.64.0.0/10`) and deprecated RFC 3879
    IPv6 site-local (`fec0::/10`) were classified as public, as were IPv4
    addresses embedded in IPv6 literals — `64:ff9b::7f00:1` (RFC 6052 NAT64
    carrying `127.0.0.1`), `::10.0.0.5` (RFC 4291 IPv4-compatible) and
    `::0:5efe:a00:5` (RFC 5214 ISATAP). The check now unwraps every
    IPv4-embedding IPv6 format and then classifies positively.
  - **Legacy numeric host forms bypassed the check entirely.** `ip_address()`
    raises on them, and the code read "raised" as "this is a DNS name, accept
    it" — while a stock resolver reads `2852039166` and `0xA9FEA9FE` as
    `169.254.169.254` and `0177.0.0.1` as `127.0.0.1`, with no DNS lookup
    involved. Such hosts are now canonicalised (not resolved) and classified as
    the literals they are.

  Genuinely public addresses are unaffected, including public addresses carried
  inside an embedding format (`::ffff:8.8.8.8`, `64:ff9b::808:808`) and addresses
  that merely resemble one: ISATAP is matched on its full RFC 5214 §6.1
  interface identifier (`00-00-5E-FE`, or `02-00-5E-FE` with the u/g bit set),
  not on the `5efe` hextet alone, so an ordinary global address such as
  `2606:4700::1234:5efe:a00:5` is still accepted.

  Two limitations remain, both name-shaped and both tracked in #211: a DNS name
  is accepted **without being resolved**, and so is a trailing-dot IPv4 such as
  `169.254.169.254.`, which POSIX `inet_aton` also rejects as an address. The
  DNS-name limitation is now stated in the docstring, `README.md` and
  `SECURITY.md` instead of being implied away. The error message no longer claims
  the host "must be public", since only IP literals are ever checked. (#210)

## [2.7.1] - 2026-08-30

### Changed
- **A policy file setting `max_tools_per_server: 0` is now rejected, and via
  #202 that terminates startup.** `LimitsPolicy.max_tools_per_server` is bounded
  at `ge=1`; `0` — and any negative value — no longer validates. **This can stop
  a gateway that starts today.** If your policy file (`.mcp-gateway-policy.yaml`
  / `.json`, or `~/.claude/gateway-policy.yaml` / `.json`) contains the literal
  line `max_tools_per_server: 0`, the gateway will refuse to start with
  `Invalid policy file ...`; remove the line to take the default of `100`, or
  set the number of tools you actually want indexed. Such a gateway indexed no
  tools at all before, so the value is unlikely to be in deliberate use — but
  the failure is a hard one and worth searching for. 2.7.0 shipped the log fix
  for this value and deliberately left the bound out: at that time a discovered
  policy that failed validation was discarded in favour of the allow-all
  default, so rejecting one value would have silently discarded the operator's
  entire policy file. #202, in the same release, made that case fatal, which is
  what makes the bound safe to add. `ClientManager`'s `max_tools_per_server`
  constructor parameter is a separate, programmatic axis and is unchanged — it
  still accepts `0` and still logs that nothing was indexed. (#207)

## [2.7.0] - 2026-08-29

### Added
- The truncation boundary at `max_tools_per_server` is now pinned by tests at
  `limit - 1`, `limit` and `limit + 1`. Mutating the guard from `>=` to `>` —
  which lets a server put one more tool in the catalog than the bound allows —
  previously survived the whole of `tests/test_client_manager.py`. (#175)

### Changed
- **A policy file found by auto-discovery that parses but is not a valid policy
  now terminates startup instead of being discarded.** This can stop a gateway
  that starts today, and that is the point: the discarded policy was replaced by
  the default `GatewayPolicy()`, and that default is **allow-all** — every field
  is a `default_factory`, so a policy with one mistake in it did not degrade to a
  partial policy, it degraded to no policy. Allow/deny lists, limits and
  redaction all silently reverted to permissive behind a single warning line that
  did not say so. The condition is exact: the file exists, `yaml.safe_load` /
  `json.loads` returned **without raising**, and the result is not a valid
  `GatewayPolicy` — which includes a list root, a scalar root and an empty YAML
  file, all of which parse cleanly and fail only the object schema. A file the
  parser *rejects*, or one that cannot be read at all, still warns and continues
  as before: it could be a half-written file, an unrelated `.json` at the repo
  root, or a merge conflict, and that fallback is deliberate. The surviving
  warning now states plainly that no policy is in effect. Explicit `--policy` /
  `PMCP_POLICY` is unchanged — it was already fatal for every mode. (#202)
- **The default policy search paths now follow the working directory.** The two
  project-local entries in `DEFAULT_POLICY_PATHS` were joined with `Path.cwd()`
  at module import, freezing the directory as of first import; a gateway that
  changed directory before constructing its `PolicyManager` looked for a policy
  somewhere else, found none, and ran unrestricted — by that road with no warning
  at all, since "no policy file" is legitimately silent. They are resolved at
  construction now. The module attribute remains patchable for tests, and
  absolute entries pass through unchanged. (#202)
- **A downstream tool that declares no `inputSchema` is now skipped instead of
  indexed.** This is a behaviour change, and the only one in this set. The
  indexer used to substitute `{}` for a missing `inputSchema` — and `{}` is not
  "we do not know", it is "any arguments at all are valid", published under the
  server's name to every caller and every model reading the catalog. MCP
  requires `inputSchema` on a tool, so a tool without one is a tool we could not
  read, and such an entry now takes the same route as any other unparseable
  entry: skipped, logged, costing only itself. A listing in which *no* tool
  parses is treated as a failed listing, so the server's previous tools are kept
  rather than reported removed. An explicitly empty `inputSchema: {}` is still
  accepted — the server said "any arguments", and that is an answer; only the
  absence, and any non-object value such as `null`, is unreadable. A server that
  omits `inputSchema` therefore loses that tool from the catalog where it
  previously appeared with a permissive schema. (#175)

### Fixed
- **`derive_npm_flags.py --verify` no longer reports host-enumerated npm config
  types as table drift.** The npm flag tables are the node-less fallback for
  npm package identity, and `--verify` is what keeps them honest against a real
  npm — but it was green on one machine and red on another with identical npm
  and identical source. npm builds `local-address`'s declared `type` from
  `os.networkInterfaces()`, so its 51 members here are facts about *this*
  machine; and when `networkInterfaces()` throws, npm's `getLocalAddresses()`
  catches it and returns exactly `[null]`, which the member rule stripped to
  nothing and reported as `value: --local-address in table, absent from live
  npm`. A drift check with false positives gets ignored, and an ignored check
  is how real drift ships.

  Detection now happens in the node script, the only place the raw members
  still exist — the serializer maps every string member to `'<literal>'`, so no
  Python-side predicate could tell 51 addresses from `loglevel`'s 8 fixed
  words. It uses npm's own `typeDescription === 'IP Address'` label (the one
  signal that survives the `[null]` case) with a `net.isIP` member scan as an
  independent backstop, and `classify()` then returns `value` regardless of the
  members. The flag is **not** exempted from the comparison: skipping it would
  blind the check to a real arity change on the flag most likely to drift, so
  `--verify` reports which flags it normalised instead — without printing the
  member count, which is the host fact. The committed tables are unchanged;
  this fixes the comparison, not the data.

  **Scope, so the next red `--verify` is not waved off as another false
  positive:** this makes the *comparison logic* host-independent, not the
  tables' *freshness*. Version skew — tables derived from one npm, checked
  against a newer one — still turns `--verify` red, correctly and by design.
  That is the signal the check exists to produce. What is gone is only the
  redness that two machines running the *same* npm could disagree about. A CI
  test now also holds the recorded schema fixture and the committed tables to
  each other, so regenerating one without the other cannot pass silently.
  (#193)
- **`_index_tools`/`_index_resources`/`_index_prompts` no longer overstate the
  catalog.** `_index_resources` documented that "the count returned is what was
  actually indexed, not what was offered", while all three returned the length
  of the parsed list. Two entries sharing an identity are two list items and one
  catalog key, so the count was wrong by exactly the number of collisions. The
  count is now of entries that actually landed, and each collision is logged at
  DEBUG naming the id. (#175)
- **`adopt_process` now clears the server's catalog entries before indexing**,
  like every other path into the indexers. Adopting a server previously indexed
  under the same name left the earlier listing's tools in the catalog beside the
  new ones — entries the adopted process does not serve, still routable. (#175)
- **A `max_tools_per_server` of `0` is no longer reported as a malformed
  listing.** A zero limit empties the parse result before any entry is examined,
  so reconciliation announced "Every tools entry in the listing was
  unparseable" — blaming the downstream for a decision the gateway's own policy
  file made. Both that message and the parser's truncation warning now name the
  limit. Deliberately *not* fixed by adding a schema bound: `LimitsPolicy` still
  accepts `0`, because at the time policy auto-discovery swallowed validation
  errors and fell back to an allow-all default, so rejecting the value would
  have silently discarded the operator's entire policy file. #202 — see the
  entry above, which ships in this same release — has since made that case
  fatal, so `Field(ge=1)` is now safe to add; it is left to a follow-up rather
  than folded in here. (#175)

## [2.6.0] - 2026-08-27

### Added
- **The release-path workflow guards now run in CI.** `release.yml` triggers
  only on tag push — the tag push *is* the publish — so it never appeared in a
  PR check, and every guard that protected the last change to it was run by
  hand, once. Two new `test.yml` jobs close that:

  `workflows` runs `scripts/check_workflows.py`, which asserts by **invariant**
  that `release.yml`'s trigger set is **exactly** `push.tags: ["v*"]` — no other
  event and no branch or path filter alongside it, since `publish` holds
  `id-token: write` against an environment with no protection rules, so any
  extra trigger makes trusted publishing reachable from it — and that no
  workflow file other than `release.yml`/`docker.yml` is tag-triggered at all;
  the `build → publish → github-release` ordering; `environment: release` **by
  name**; no `if:` or `continue-on-error` at job *or* step level on any of the
  three jobs (skipping `build` skips `publish` through `needs`, and the tag push
  still concludes green); the
  exact committed `permissions:` maps at both workflow and job level, the exact
  committed `uses:` references, and exactly the three expected jobs; that every
  job in every workflow carries a `timeout-minutes` of 10–30 (a bare
  `timeout-minutes: 360` re-creates the six-hour default); and by **drift**
  that no job present in the PR base has disappeared from any changed workflow,
  including one deleted outright. Drift **fails closed**: a base ref that does
  not resolve, and a `git diff` that fails for any other reason (a base sharing
  no history with HEAD exits 128), are failures, never "nothing changed". It
  also runs a digest-pinned actionlint.

  `release-diff-ack` covers by **acknowledgement** what an allowlist cannot
  cover by enumeration: any PR touching `release.yml` fails unless it carries a
  `release-change-approved` label, read live from the API rather than from the
  event payload frozen at trigger time.

  **Not covered, deliberately:** a timeout above the 10-minute floor but below a
  job's real p100; environment protection rules, which live in GitHub settings
  and are invisible to any file check; and `if:`-skipping the guard job itself,
  which GitHub counts as *satisfying* a required check. Per-mutant exit codes,
  including the ones that stay green, are in
  `.consiliency/evidence/mutation-189.md`.

  Because the `uses:` and `permissions:` allowlists are exact, a legitimate
  edit to `release.yml` — bumping an action, granting a scope — must update the
  constants in `scripts/check_workflows.py` in the same PR. That is intended.


### Fixed
- **npm package identity now comes from npm's own parser where it is certain, and
  is refused otherwise.** The hand-written npm flag tables have been repaired five
  times (Consiliency/pmcp#180 → #192 → #194 → #195 → the 2.5.2 nullable-boolean
  spelling), and every defect was in the rules *around* the tables rather than a
  missing entry — so every repair produced a *confident wrong answer*, which the
  freshness gate reads as positive confirmation that a cached tool description
  still describes the configured package.

  For an `npx`/`npm` server the gateway now asks the host npm's own `nopt`, its
  own `@npmcli/config` definitions and its own `npm-package-arg`, through a
  faithful port of npm's `npx-cli.js` pre-scan, and accepts the answer only when
  nothing in the invocation could redirect resolution. It **refuses** when:

  - the parsed configuration contains any key beyond `--yes` and `--package`
    (`--registry`, `--userconfig`, `--prefix`, `--cache`, `--call`, `--workspace`,
    a shorthand such as `--silent` that expands to `--loglevel`, or an unknown
    flag) — this is an allowlist of plain shapes, not a denylist of dangerous
    ones;
  - the server's environment **overlay**, or the gateway's own process
    environment, sets `npm_config_*` (case-insensitive), `PATH`, `HOME`,
    `NODE_PATH`, `NODE_OPTIONS`, `PREFIX` or `NVM_*`;
  - walking up from the effective working directory, npm would set a local
    prefix (a `package.json` or `node_modules` in any ancestor), because a
    project `.npmrc` can rename the package and a local `node_modules/.bin` entry
    means npm never reaches the registry at all;
  - `npm-package-arg` reports anything but a registry spec — notably an **alias**
    (`npx -y myalias@npm:left-pad` really runs `left-pad`, and the alias name is a
    squattable different package);
  - the npm subcommand has no package operand (`npm run`, `npm start`, `npm test`,
    `npm create`, a typo, bare `npm -y pkg`, and now `npm dlx`, which is
    pnpm/yarn spelling and is not an npm command at all);
  - the spawn-time self-test against the host's own parser fails, `bin/npx-cli.js`
    is not one this port was verified against, or npm's parser cannot be loaded.
    A failed self-test **refuses** — it does not fall back to the tables, because
    a failed self-test is precisely the evidence that the tables' model of npm is
    wrong. One WARNING is logged.

  Refusing costs auto-update coverage for an unusual configuration: the server
  keeps running, but its package is reported as `unknown`, so its descriptions
  refresh every cycle and `gateway.update_server` cannot name a package for it.
  Measured cost on the shipped manifest: **zero** — all 79 npm-family servers use
  the plain `npx -y <pkg>` shape and all 79 resolve to the same package the real
  `npx` binary fetches.

  Where node is not installed the flag tables remain in use unchanged, which is
  the behaviour every release through 2.5.1 shipped.

  **Known residual:** a `package=` or `registry=` line in a **user or global**
  `.npmrc` changes what npm resolves and the gateway cannot see it. Project-level
  `.npmrc` is covered by the local-prefix refusal, and `npm_config_*` in the
  gateway's own environment is covered by the process-environment check; the
  user/global rc file is the one input that remains unguarded.

  `detect_package_type`, `_npm_package_arg`, `get_package_version` and
  `gateway.update_server`'s pin detection all take the server's environment
  overlay and working directory as **required** parameters now, since both are
  identity inputs.
## [2.5.2] - 2026-08-26

### Fixed
- **Six npm flag spellings read a literal `null` as the package name.** 2.5.1
  added a table of the boolean flags that take a literal `null` as their *value*
  rather than as the package name — `null` is a real published npm package, so
  the distinction decides a server's identity, and `refresher.py`'s freshness
  gate treats a matching identity as **positive confirmation** that a cached
  tool description still describes the configured package. That table was
  written by hand with 12 entries. npm has five nullable boolean definitions
  (`yes`, `optional`, `production`, `workspaces`, `expect-results`) but
  **eighteen** spellings for them, because `y`, `ws`, `n` and `no` are
  shorthands — `n` and `no` both expand to `--no-yes` — and each is legal in
  both its `-x` and `--x` form. The six that were missing are **`--y`, `-ws`,
  `-n`, `--n`, `-no`, `--no`**: under 2.5.1 each of these read the following
  `null` as the package name, so `npm exec -n null server-a` and
  `npm exec -n null server-b` both resolved to the package `null` and could be
  served each other's cached tool descriptions.

  **Not a regression between releases** — 2.4.1, 2.5.0 and 2.5.1 all resolve
  these six to `null`; verified by running each released version's
  `detect_package_type` directly. The blanket rule that briefly handled them
  correctly existed only on `main` between two unreleased commits, so no shipped
  version was ever right about them. 2.5.2 is the first.
- **The set is now generated, not hand-listed.**
  `.consiliency/notes/derive_npm_flags.py` derives it from npm's own
  `@npmcli/config` definitions: a spelling is nullable iff its resolution
  target, after shorthand expansion and after stripping a leading `no-`, is a
  definition whose declared type includes `null`. It was the fourth defect in
  this parser traceable to hand-transcribing npm's behaviour.
- **`--verify` now covers this table**, which previously had no drift
  protection at all. Each definition is probed with a literal `null`, and every
  one of npm's 442 enumerable flag spellings is run through npm's own parser as
  `npm exec <flag> null zz` — a definition-level check alone would not have
  caught a spelling omission. The new check rejects the shipped 2.5.1 table
  with exactly six mismatches.

  This does **not** close the broader gap tracked in
  Consiliency/pmcp#195: attached values (`--global=pkg`), npx's own `-p=`
  rewriting, and npx's `-n` removal are still unhandled.

## [2.5.1] - 2026-08-26

### Fixed
- **npm no longer reads a flag's *value* as the package name.** npm was the last
  of the five ecosystems still failing *open* on an unrecognised flag: the scan
  skipped anything starting `-` and took the next bare token, so
  `npm exec --loglevel silly server-a` and `… server-b` both resolved to
  `silly`. Because 2.4.0's identity gate treats a matching name as a **positive
  confirmation**, two unrelated servers collapsed into one identity and one was
  served the other's cached tool descriptions. `--registry`, `--global false`
  and `--color always` collided the same way.

  npm's flag arity is now generated from npm's own config schema
  (`@npmcli/config`'s `definitions` and `shorthands`, 181 flags and 40
  shorthands) rather than transcribed by hand, and an unlisted flag makes the
  scan report no identity instead of guessing. Shorthands are expanded from
  npm's own map, so `npm --silent exec <pkg>` keeps resolving and the previous
  hand-coded `-y` special case is retired. Two flags whose arity depends on the
  *next token's content* — `--color` and `--browser` — are deliberately
  unlisted and therefore refused.

  **The cost, deliberately:** a server launched with a flag npm's own schema
  does not describe can no longer be auto-updated. `gateway.update_server`
  refuses it by name and command line rather than probing. An omission costs
  auto-update for one unusual config — visible, and fixable by adding the flag
  — where the previous "take the next token" default produced a silent
  collision instead. No bundled manifest server is affected: all 98 launchable
  entries resolve exactly as before.

  Two places where the boolean rule is subtler than "a switch takes no value":

  - **`null` is a real published npm package.** npm's parser consumes a literal
    `null` after only the five *nullable* booleans (`--yes`, `--optional`,
    `--production`, `--workspaces`, `--expect-results`); after any other
    boolean, `null` is the package. The rule is scoped to those five, so
    `npm exec --global null pkg-a` and `… pkg-b` stay distinct.
  - **`npx` behaves the opposite way to `npm`.** It pre-scans its arguments and
    inserts `--` before the first positional, so a boolean switch there consumes
    nothing — verified against the real binary: `npx --global true pkg` runs the
    package `true`. Because the `--no-` family differs again, `npx` reports no
    identity for these forms rather than a modelled guess.

  Known remaining gap, tracked in
  [#195](https://github.com/Consiliency/pmcp/issues/195): the same class
  survives in rarer spellings — an attached value (`--global=pkg`), nopt's
  abbreviation matching (`-n`, `--y`), and `npx`'s own rewriting of `-p=` and
  removal of `-n`. No server in the bundled manifest uses any of them.

## [2.5.0] - 2026-08-26

### Fixed
- **A command-line flag's *value* is no longer mistaken for the package name.**
  Package detection skipped flags but not the tokens those flags carry, so the
  first "non-flag" argument was routinely a flag's argument. `uvx --python 3.12
  pkg-a` and `uvx --python 3.12 pkg-b` both resolved to `3.12`, and because
  2.4.0's identity gate compares exactly this name to decide whether a cached
  description still describes the configured package, an equal name read as a
  **positive confirmation** — serving one package's tool descriptions for a
  different package indefinitely. Seven forms were affected: uvx `--python` and
  `--with`, pip `--index-url`, cargo `--features`, docker `--env-file` and
  `--mount`, and `npm exec --package=<pkg> -- <bin>` (which returned the
  *binary*, so two packages exposing the same binary confirmed as one).

  Flags are now classified per ecosystem as value-taking, boolean, or
  *positive* (`uvx --from`, `cargo -p`, `npm --package` — where the value **is**
  the package), with tables transcribed from each tool's own `--help`.

  Three user-visible consequences:

  1. **Affected servers refresh once.** Their cached identity changes, the same
     one-time migration 2.4.0 and 2.4.1 made.
  2. **A server launched with a flag pmcp does not recognise can no longer be
     auto-updated.** This is the cost, and it is deliberate: anything unlisted
     now reports no identity rather than guessing. An omission costs
     auto-update for one unusual config — visible, and fixable by adding the
     flag — where the previous "take the next token" default produced a silent
     collision instead. `gateway.update_server` refuses such a server by name
     and command line rather than probing.
  3. **The identity gate now actually holds for the forms #180 left open.**

- **`uvx --from` values are read as PEP 508 requirements.** `browser-use[cli]`
  resolves to `browser-use` and `index-it-mcp==1.2.0` to `index-it-mcp`. This
  repairs a live defect: the bundled manifest ships `--from browser-use[cli]`,
  and a PyPI lookup for that literal string returns nothing, so that entry's
  version checks had been silently failing. A `git+https://…` value keeps the
  whole URL as its identity — distinct URLs are distinct packages.

- **`gateway.update_server` no longer misreads a uvx version pin.** Pin
  detection now shares one scan with package detection instead of skipping
  every `-`-prefixed token and reading `==` off the first bare one. That was
  wrong in both directions: `--from=pkg==1.2.0` reported *no pin*, so an
  explicitly pinned server could have been moved to the latest version, while
  `--with requests==2.0 pkg` reported an injected dependency's version as the
  server's own pin and refused an update that was never pinned.

  The README's documented pin form — `uvx --python 3.12 --from
  index-it-mcp==1.2.0 index-it-mcp` — previously identified the package as
  `3.12`; it now resolves correctly and needs no configuration change.

## [2.4.1] - 2026-08-25

### Security
- **`gateway.update_server` no longer installs and executes a registry package
  derived from a misparsed command.** The update probe is built from the parsed
  package name — `npx -y {name}@latest --help` — and `npx -y` installs without
  prompting. A server configured as `npm run mcp` names a **script** in the
  local `package.json`, not a registry package, but the parser returned it as
  one, so pmcp fetched and ran whatever occupied that name on the public
  registry. Short generic script names (`run`, `start`, `dev`, `mcp`) are
  exactly the kind that can be registered and waited on.

  npm package detection is now an **allowlist**: only `exec`, `x`, `install`,
  `i`, `add` and `dlx` put a registry package in the next position. Every other
  subcommand — `run`, `start`, `test`, `stop`, `restart`, `run-script`, `init`,
  `create` — and every misspelling of one reports **no recoverable package
  identity**, and `update_server` refuses on that before constructing any
  probe. That is the same rule the identity gate follows: cannot confirm, so do
  not act on a guess.

  An allowlist rather than a denylist of script runners, because the
  consequence of being wrong is asymmetric: failing closed costs only the
  ability to auto-update a server launched by an unusual form, while failing
  open costs arbitrary package execution. (`npm create foo` also shows why
  synthesising a name is not safe: npm resolves it to the package `create-foo`,
  so `foo` names a *different* package than the one npm would run.)

  Reaching this required a server configured with an affected form **and** an
  operator invoking `gateway.update_server` on it; it was not remotely
  triggerable. `npx -y run` still resolves normally — the refusal is scoped to
  npm subcommands whose operand is not a package, not to those names.

### Fixed
- **Two different packages no longer share one identity for common `docker` and
  `npm` command forms.** 2.4.0's identity gate decides whether a cached
  description still describes the configured package by comparing the *name*
  `detect_package_type` returns, so a name that was stable across two different
  packages was read as a positive confirmation — and the freshness
  short-circuit went on serving the wrong package's tool descriptions.

  Two independent causes, both closed:

  - **Docker references split on the first `:`,** so `registry:5000/old-image`
    and `registry:5000/new-image` both resolved to the image `registry` — the
    registry host, not an image at all. A colon only introduces a tag when it
    appears in the final path segment; before the last `/` it is a registry
    `host:port`. The correct rule already existed in this module as
    `_docker_image_tag`, so the fix adds its paired complement rather than a
    second, divergent implementation of the same rule.
  - **`npm` subcommands were taken as the package name,** so `npm exec old-pkg`
    and `npm exec new-pkg` both resolved to `exec`. A leading subcommand
    (`exec`, `x`, `run`, `install`, `i`, `add`, `create`, `dlx`) is now skipped
    — once, and only for `npm`, so `npm install i` still finds the real package
    `i` and `npx -y exec` still finds a package genuinely named `exec`.

  **Affected servers refresh once.** A docker server on a `host:port` registry
  or an `npm exec` server now has a *different* package identity than the one
  its cache entry recorded, so that entry fails the identity check once and is
  regenerated — the same one-time migration 2.4.0's `package_type` addition
  caused.
- **A docker digest is now recognised as the pin it is.** `gateway.update_server`
  read the tag from the whole reference, so `img@sha256:abc` reported a pin of
  `abc` — a fragment of the digest presented as a version — and
  `img:1.2@sha256:abc` reported `1.2@sha256:abc` instead of a usable value. A
  digest is the *tightest* pin docker offers, so it is now reported whole and
  checked before the tag: `img@sha256:…`, `img:1.2@sha256:…` and
  `img:latest@sha256:…` all report the digest. That last form matters — a
  `latest` tag must not discard a real digest pin. Previously such a server
  could be "updated": pmcp would pull `image:latest`, restart the unchanged
  digest-pinned configuration, and record the registry's newest digest while
  still running the old immutable image.
- **A version pin on an `npm exec` server is now detected.** Pin detection
  shares its argument scan with package detection, so it inherited the
  subcommand bug: `npm exec pkg@1.2` scanned to `exec`, which carries no
  version suffix, and a real pin was reported as unpinned.

  **This narrows Consiliency/pmcp#180 rather than closing it.** Package identity
  is still collapsed wherever a flag's *value* is taken as the package name —
  `docker run --env-file X <image>`, `docker run --mount <spec> <image>`,
  `npm exec --package=<pkg>`, and the `uvx`/`pip`/`cargo` equivalents. Those are
  tracked on Consiliency/pmcp#182, and Consiliency/pmcp#183 tracks a related but
  more serious consequence of a misparse.

## [2.4.0] - 2026-08-25

### Fixed
- **A cached description is now checked against the package that is actually
  configured, not just against its version.** All three refresh sites —
  `refresh_server`'s up-to-date short-circuit, `refresh_all`, and
  `check_staleness` — paired a cached entry with a server config by **name** and
  then decided freshness by comparing **versions** alone. Nothing asked whether
  the cache still described the same *package*, so swapping the configured
  package at an equal version served the wrong package's tool descriptions
  indefinitely: a cache for `old-pkg@1.0.0` against a config for `new-pkg@1.0.0`
  looked current forever. The docker case was already covered — a version
  against a digest is `incomparable`, which is not `not_newer` — but a
  same-ecosystem swap and an npm ↔ pypi ↔ cargo swap were not.

  Identity is resolved before the comparison at all three sites now. **An
  unknown side means "cannot confirm identity", and that resolves to
  refresh — never to "cannot compare, so skip the check."** The second phrasing
  is the natural one to reach for and is the same fail-open collapse as
  `not is_version_newer(...)`, which shipped three times
  (Consiliency/pmcp#155, #156, #163) before it was made unrepresentable.

  The cached entry gained a `package_type`, because `package` is a bare name
  carrying no ecosystem and npm, pypi and cargo all produce orderable *release*
  versions — so npm `foo@1.0.0` and pypi `foo@1.0.0` were indistinguishable to
  a name comparison. A cache written before this release has no type, reads as
  unknown, and refreshes once. Nothing has to be migrated by hand and the cache
  format needs no version bump.

  One cosmetic consequence: a stale report is a `(cached_version,
  latest_version)` pair, so an entry that is stale by *identity* at an equal
  version prints `srv: 1.0.0 -> 1.0.0`. Confusing to read, but not wrong — that
  entry genuinely does need regenerating.
- **`pmcp refresh --check-versions` now honours `--cache-dir`.** `run_refresh`
  computed the cache path from `--cache-dir` and then called `check_staleness()`
  with no arguments, dropping it, so the check silently inspected the default
  cache instead of the one that was asked for. Anyone pointing
  `--check-versions` at a non-default cache was reading a different file than
  they named, with nothing in the output to say so.

### Changed
- **`pmcp refresh --check-versions` now reports a server whose package it cannot
  look up separately, as unconfirmed rather than as stale.** A server launched
  as `node /opt/srv.js` or `python -m thing` has no classifiable package, so its
  configured identity is unknown, and "cannot confirm identity" resolves to
  refresh. That is the right rule — the only alternative is to read "cannot
  classify" as "assume it matches", which is the fail-open reading that let a
  swapped package look current in the first place — but it would have made such
  a server appear under "servers with newer versions" on every run, permanently,
  beneath a `Run 'pmcp refresh --force' to update.` footer that could not settle
  it, since the next check still cannot classify the package. So the report is
  now split. Servers with a genuinely newer version keep the existing output and
  that footer; servers whose current version could not be looked up are listed
  under their own heading which says so, notes that this is not the same as
  being out of date, and points out that their descriptions are regenerated by
  the next plain `pmcp refresh`. Previously these servers were skipped in
  silence. A manifest of only registry-installed (npm/pypi/cargo/docker) servers
  is unaffected.
- **`refresh_all` now drops an unclassifiable server's cached descriptions when
  regeneration fails, where it previously kept them.** A server that fails the
  identity gate has its cached entry discarded up front and is regenerated; if
  that regeneration then fails — the server does not start, a version lookup
  times out — neither the failure fallback nor the final merge puts the old
  entry back, and the server is left with no cached descriptions until a later
  refresh succeeds. For a `node`/`python` server this costs something real:
  there was never *evidence* of a package mismatch, only an inability to confirm
  one, so a transient startup failure now costs descriptions that were probably
  still accurate. Writing back descriptions that may describe a different
  package is the outcome this release exists to prevent, so the trade is
  deliberate — but it is a trade, not a free win.

## [2.3.0] - 2026-08-24

### Fixed
- **Catalog listings are now read to the end.** `tools/list`, `resources/list`
  and `prompts/list` were sent once with no cursor and whatever came back was
  taken as the whole catalog, so a downstream with more entries than its page
  size had the rest silently missing. That truncation predated downstream
  fan-out, but once reconciliation began publishing `list_changed` it started
  asserting the catalog was current over a partial view — and re-applying the
  truncation on every downstream notification rather than once at connect.
  `nextCursor` (and the `next_cursor` spelling) is now followed to the end.

  A failure on **any** page makes the whole kind unreadable rather than
  partial: prior entries are kept and nothing is published. Merging the pages
  that did arrive would drop entries the server still has and announce the drop
  — the same false-removal shape this module has been corrected for repeatedly.
  Following the cursor is bounded, and a server that repeats a cursor or never
  stops paginating is treated as unreadable rather than looping or indexing a
  truncated view.

  Connect and refresh share this path, so they read complete listings too.
  One consequence worth stating plainly: a server whose `tools/list` fails on a
  *later* page now connects successfully with an empty tools catalog, where
  before pagination existed there was no later page to fail. Only a page-one
  failure is still a connect error. That matches how connect already treats a
  first page whose every entry is unparseable, but such a server sits at zero
  tools until a downstream notification or a refresh reconciles it.
- **A downstream server's JSON-RPC `error` object no longer loses its `code`
  and `data`.** Both dispatch paths kept only `message`, discarding the rest
  of the `error` member. `ClientManager` now raises a typed `DownstreamError`
  carrying `code` and `data` alongside `message`. Scoped to the
  `ClientManager` boundary — `gateway.invoke` still maps every exception to
  `E302` through `str(e)`, which is byte-identical to the old message, so no
  `gateway.*` output changes; surfacing `code`/`data` to MCP clients is
  tracked separately.

### Added
- **A downstream server's own `notifications/tools/list_changed`,
  `notifications/resources/list_changed`, and `notifications/prompts/list_changed`
  now reach subscribed clients.** Previously the read loop parsed these frames
  and silently dropped them on both transports — a notification has no `id`, so
  it fell through the pending-request gate with no `else`. A downstream server
  that added or removed a tool at runtime was invisible until the next
  `gateway.refresh()`; that gap was v11 P3B's own Non-Goal.

  This is reconciliation, not forwarding: `ClientManager`'s indexes back
  `gateway.catalog_search`, `gateway.describe`, and `gateway.invoke`, so
  relaying the raw notification to the subscription sink would have told a
  client "refetch" and handed it the *old* catalog — with a tool the server
  just removed still invocable. The gateway now re-indexes the announcing
  server first and publishes only once that finishes, and only for the catalog
  kinds that actually changed. Changed by *content*, not by identifier and not
  by count: a rename publishes, and so does a tool whose description or input
  schema was edited under an unchanged name.

  Reconciliation fetches first and swaps second. It lists the server's tools,
  resources, and prompts without touching the catalog, then removes and
  re-indexes in a single synchronous block that contains no `await` — so a
  `gateway.invoke` arriving mid-reconcile sees either the whole old catalog or
  the whole new one, and never the empty window in between. A downstream that
  announces a change and then fails `tools/list` therefore costs nothing:
  nothing was removed, so there is nothing to roll back, and nothing is
  published. Each kind is handled independently — a `resources/list` that
  fails (which is also how a server that simply does not implement resources
  answers) leaves the existing resources in place and publishes nothing for
  them, while the kinds that did answer still reconcile normally. **The
  guarantee for a subscribed client:** the catalog is reconciled *before* the
  notification goes out, so a client that refetches on receipt sees the change,
  every time.

  A malformed catalog entry costs only itself. Indexing guards each entry
  individually, so one tool the gateway cannot parse is logged and skipped
  while the rest of that listing is indexed normally — the entries before it
  *and* after it. This matters most on the reconcile path, where the swap has
  already removed the server's previous entries by the time indexing runs: an
  exception escaping there would have left the server with no catalog at all,
  permanently, because the read loop stays healthy and no reconnect arrives to
  heal it. `gateway.refresh` and connect-time indexing reach the same code, so
  this is a deliberate connect-time behaviour change too: **a server with one
  unparseable tool now connects with the rest of its catalog instead of failing
  outright.**

  A listing whose entries are *all* unparseable is treated as a failed listing,
  not as an empty one. Offered entries of which not one survives parsing leaves
  the gateway in the same epistemic state as a request that failed — it could
  not read the answer — so that kind keeps its previous entries and publishes
  nothing. Failing to parse a listing costs visibility of the server's catalog;
  it does not stop the server's tools from working, and announcing a removal on
  the strength of it would tell every subscribed client those tools are gone.
  The boundary is the count offered, not the count indexed: a listing that
  offers **zero** entries is a genuine answer — the server emptied that kind —
  and still clears the entries and publishes.

  A reply the gateway cannot read is a failed listing too, and an **absent**
  collection is not an empty one. A `tools/list` reply of `{}` — missing the
  `tools` array the protocol requires — is malformed, not an announcement that
  the server has no tools, and the same goes for a reply carrying something
  other than an array in its place (`{"tools": {}}`, `{"tools": null}`). Each
  of those now keeps the kind's previous entries and publishes nothing, exactly
  like a request that failed; only a genuine array is an answer, and an empty
  array still clears. Per kind, still: an unreadable `tools` reply no longer
  costs an honest `resources` answer arriving in the same pass.

  A catalog entry carrying no identity fails to parse rather than acquiring
  one. A resource with no `uri`, or a prompt or tool with no `name` (or an
  empty one), used to be indexed under a synthesized identifier of the form
  `server::` — a catalog entry the downstream never offered, which replaced the
  real entries and was published as a change. Such an entry is now skipped like
  any other unparseable one, and a listing of nothing but those falls under the
  all-unparseable rule above and keeps the previous entries.

  Failure classification is conservative by design. Any failure to list a kind
  — a transport error, a server that does not implement it, a reply whose
  collection is absent or is not an array, or a listing that could not be
  parsed at all — keeps that kind's previous entries; only an explicit empty
  answer clears them. The accepted cost is the mirror case: a
  server that drops a capability mid-session and never reconnects keeps stale
  entries in the catalog, which then fail loudly at invoke time. That is the
  deliberate trade — a stale entry that errors when called is recoverable and
  self-announcing, whereas a falsely removed entry is invisible: it silently
  disappears from every subscribed client's catalog with nothing to point at.

  Reconciliation runs as a spawned, per-server-coalesced background task
  rather than inline in the read loop — re-indexing awaits a response that the
  very read loop which received the notification is responsible for
  resolving, so an inline await would deadlock the connection instantly. A
  downstream that emits `list_changed` in reply to reconciliation's own
  `tools/list` is bounded by a debounce on the re-run, not just coalescing, so
  it costs one extra reconcile per interval instead of a hot spin. Both
  transports are covered: stdio (`_handle_stdout_line`) and streamable
  HTTP/SSE (`_read_sse`) previously shared the same silent-drop, and both now
  dispatch through the same reconcile path. Unrecognised `notifications/*`
  methods (progress, logging) remain a no-op, as before.

### Changed
- **`version_checker.compare_versions(current, latest, package_type)` is now the
  sole version-classification path; `is_version_newer` and
  `are_versions_comparable` are deleted, not deprecated.** The old pair
  answered "is X newer" and "can X and Y be ordered at all" as two separate
  booleans, and `is_version_newer` failed closed, so its `False` meant either
  "up to date" or "cannot be ordered" — the same ambiguity `are_versions_comparable`
  existed to guard against. A caller combining them as
  `are_versions_comparable(...) and not is_version_newer(...)`, or skipping the
  guard and just negating, collapsed those two meanings back into one `False`.
  That exact collapse shipped three times (#155, #156, #163), and an AST lint
  written to police the pattern was bypassed by reviewers four times, because a
  syntactic check cannot prove a dataflow property. `compare_versions` returns
  a three-way `Literal["newer", "not_newer", "incomparable"]` instead, so a
  caller has to name the branch it means. Deleting the two wrappers — rather
  than leaving them as deprecated aliases — is what makes the collapse
  unrepresentable instead of merely detectable: a function that no longer
  exists cannot be negated into the old ambiguity. The AST lint is deleted
  with them, since there is nothing left for it to police. `is_version_orderable`
  is unaffected and remains. Behavior is unchanged: all prerelease ordering,
  SemVer-vs-PEP 440 disagreement on `1.0.0-1`, build-metadata, digest
  canonicalization, CalVer, and mixed version/digest cases classify identically
  to before.

## [2.2.1] - 2026-08-23

### Changed
- **SemVer comparison now uses the `semver` library instead of a hand-written
  key** (new dependency, pure-Python, no transitive dependencies). The npm and
  Cargo lane needs true SemVer 2.0.0 precedence because PEP 440 disagrees with
  it — PEP 440 reads `1.0.0-1` as the post-release `1.0.0.post1`, SemVer as a
  prerelease *below* `1.0.0` — and 79 of the manifest's 107 servers are npm.
  This module replaced hand-rolled version logic with `packaging` precisely
  because every hand-rolled form of it produced a fabricated-notice bug; the
  SemVer lane was the last piece still hand-written.

### Fixed
- **A stale descriptions cache is no longer pinned when the FETCHED version is
  unreadable.** The "already up to date" short-circuit negates a comparator
  that fails closed, so an orderability guard was added to stop an unreadable
  version reading as current — but it checked only the cached side. With a
  cached `1.0.0` and a fetched `nightly`, the guard passed, the comparison
  failed closed, and the negation still reported "up to date", never
  refreshing.

  Checking each side individually turned out to be insufficient too:
  comparability is a property of the **pair**. `1.0.0` and `abcdef123456` are
  each orderable on their own, but a version and a digest cannot be ordered
  against each other, so the same "up to date" answer came back for a server
  whose cache entry was reused by name after its package type changed. A new
  `are_versions_comparable(current, latest, package_type)` asks about the pair,
  and that is what now guards the short-circuit.
- **An all-numeric truncated image digest is documented as incomparable
  without a package type, and callers are now pinned to pass one.**
  `get_docker_version` truncates SHA-256 to 12 hex characters, which can be all
  digits — the same shape as a calendar version like `202612180000`. Resolving
  that by guessing (promoting the numeric side when its partner is a digest)
  was implemented and rejected: the guess fabricates an update when the numeric
  side really is a calendar version, which is what `is_version_newer`'s
  fail-closed contract exists to prevent. A mixed pair therefore stays
  incomparable, and a test now enforces that every caller passes the package
  type, which is what actually resolves it.
- **A `sha256:`-prefixed digest with no hex letter is now recognised.** The
  pattern required a hex letter even when the prefix was present, so an
  all-numeric prefixed digest was rejected outright.
- **The intermittent `test_ec_p2_7_reconnect_does_not_leak_transports` failure
  is fixed at its root.** `sse_starlette.sse.AppStatus.should_exit` is a
  process-global class attribute that uvicorn's shutdown handler latches `True`
  and never resets. The fake-remote test server cleared it on teardown, which
  protects against its own shutdown but not against a server started elsewhere
  in the same interpreter — and `tests/mcp2x`, which stops uvicorn servers,
  sorts immediately before `tests/runtime`. Inheriting the latched flag made
  every SSE stream end instantly, so the test failed in CI while passing in
  isolation. The flag is now cleared on entry as well as exit, with a test that
  latches it deliberately and asserts a connection still works.
- **The CHANGELOG CI guard no longer treats a failed label lookup as "no
  label".** A live-lookup *failure* now falls back to the frozen event payload
  before concluding the `skip-changelog` label is absent, so an API hiccup
  cannot block a PR that really was labelled. A lookup that *succeeds* and
  returns no labels stays authoritative — otherwise removing the label would
  not re-enable the check.

- **`gateway.update_server` no longer risks restarting onto a different package
  than the one it probed.** The tool resolved the server's config, ran an update
  probe with a 60-second timeout, and then let `gateway.restart_server` resolve
  the config a second, independent time. The config loaders re-read from disk on
  every call, so a `.mcp.json` or manifest edit landing inside that window could
  make pmcp probe and install package A, restart onto package B, and then record
  A's version as B's — the silent-misreport class, reached through a race rather
  than through resolver divergence.

  `update_server` now re-resolves after the probe, verifies the result still
  describes the same downstream process, and restarts onto that exact verified
  config. If the configuration changed, or the server is gone from the config
  entirely, the update is refused: the package was fetched but is **not**
  activated and **no** version is recorded, with a message saying so.

  The check runs *before* `gateway.refresh()`, which is itself a diff-based
  reconcile that can disconnect and reconnect a changed server on its own —
  checking afterwards would have allowed the fetched package to be activated
  before the refusal was reported.

  The verification covers configuration-driven changes to the spawned process
  environment as well as to the command: dropping an explicit `env` entry whose
  value happens to match the ambient one would otherwise compare as unchanged
  while silently removing a PMCP-managed credential from the restarted server
  (or, reversed, newly exposing one to it). It deliberately does not freeze the
  *ambient* environment across the update — a shell or secret-store change
  during the probe affects the probe and the restart alike.

  This matters more since 2.2.0: with the automatic update notices removed,
  `gateway.update_server` is the only update path.

## [2.2.0] - 2026-08-20

### Removed
- **Automatic "update available" notices.** `gateway.describe`, `gateway.invoke`
  and `gateway.provision` no longer return `update_warning`,
  `gateway.catalog_search` no longer returns `stale_updates`, and the hourly
  background stale-version indexer is gone. **These fields are removed from the
  tool output schemas, not merely left empty** — a client reading them will no
  longer find the keys, which previously appeared as explicit nulls.

  The gateway cannot determine which version of a package a running server is
  actually executing. The recorded version was an upstream snapshot taken when
  the server was last described, so a notice could both invent an update that
  did not exist and hide one that did — particularly for a server configured in
  both the manifest and `.mcp.json`. Eight separate attempts to source a
  trustworthy version failed, and the only remaining method — running each
  server's own `--version` on a schedule — would mean executing third-party
  package code with that server's credentials to produce an advisory message.

  **`gateway.update_server` is unchanged** and remains the way to check and
  apply updates: it probes the package, reports what it found, restarts the
  server, and refuses servers pinned to a specific version.


### Fixed
- **`gateway.update_server` no longer orphans a process tree when its update
  probe hangs.** The probe runs the downstream package's own code (e.g.
  `npx <pkg> --help`) with a 60-second timeout, but the process was spawned
  without its own session and the timeout only abandoned the wait — it never
  signalled the child. A package that ignores the probe flag and runs as a
  server therefore left its whole tree alive, including grandchildren such as
  the browser `@playwright/mcp` launches, which holds the profile lock and
  breaks the next launch. The probe now spawns as a process-group leader and
  reaps the group on timeout or cancellation.

- **Update notices are no longer fabricated for servers whose version is not a
  release number.** The version comparison extracted digits from whatever it was
  given and had no way to say "I cannot read this", so a server reporting
  `nightly`, `build-1`, `main`, or an empty string compared as *older* than any
  real release and produced an "update available" notice that was never true.
  An empty version is the default for servers built on the current MCP SDK, so
  this was reachable in ordinary use.

  Comparison now uses `packaging.version` (a new dependency), which implements
  PEP 440 ordering and rejects anything that is not a release outright. Beyond
  removing the false notices, this corrects ordering that the previous
  digit-extraction approach got wrong: a pre-release such as `1.0.0-rc1` is now
  correctly older than `1.0.0` (previously the real release was hidden), and a
  build-metadata difference is no longer announced as a new release. Docker
  images, whose "version" is a content digest rather than a number, are compared
  for difference instead of order.

## [2.1.1] - 2026-08-12

### Fixed
- **`gateway.update_server` now actually restarts the server, and no longer
  leaves a stale "update available" notice behind after a successful
  update.** Two compounding bugs:
  - `update_server` called `gateway.refresh()` to activate the update, but
    `refresh()` is a diff-based reconcile that deliberately leaves a server
    whose resolved command/args are unchanged connected and running. A
    version-only update (`npx pkg@latest`, `uvx --refresh pkg`) never
    changes argv, so `refresh()` alone never respawned the process -- the
    gateway kept serving the OLD package despite `update_server` reporting
    success. `update_server` now explicitly restarts the target server
    (reusing `gateway.restart_server`'s own resolve/disconnect/connect
    machinery) and gates every downstream effect on that restart actually
    succeeding; a refused or failed restart is now reported as `ok: false`
    with a message explaining the update was fetched but not activated,
    instead of silently claiming success.
  - Separately, the recorded `version` (an upstream-latest snapshot from the
    last `refresh`/describe pass, not the installed version) stayed pinned
    at its pre-update value forever, because nothing in the update path ever
    rewrote it. Every notice path (`gateway.describe`/`invoke`'s update
    warning, `catalog_search`'s `stale_updates`, and the background stale
    sweep) kept recomputing the identical stale notice from that value --
    surviving even a restart, since the cache is reloaded from disk. Once
    the server restart succeeds, the freshly-probed version -- along with
    the tool list and generation timestamp, read live from the just-restarted
    connection -- is now written into the descriptions cache and persisted
    to disk, so both the notice and the offline tool listing stay accurate
    across restarts.
  - A third bug in the same area: `update_server` resolved its probe target
    (the command it version-checks and updates) via a manifest/discovered-only
    lookup, while the restart above resolves via the same precedence
    `gateway.restart_server` uses, where a `.mcp.json` entry overrides the
    manifest. For a server configured in both places -- README documents
    pinning a server's exact version via `.mcp.json` as the supported
    override channel -- this meant probing and version-checking one command
    while restarting a different one. Concretely: manifest has an unpinned
    `npx -y context7`, `.mcp.json` pins `npx -y context7@1.2.3`; the probe
    detects/installs upstream's real latest, the restart activates the
    *pinned* 1.2.3 process, and the (now-fixed) bookkeeping above would
    record the probed "latest" as current -- silently misreporting the
    pinned server's version. Both steps now resolve through the identical
    function, so they can never disagree, and a server whose effective
    config pins a concrete version is refused outright (`ok: false`,
    explaining the pin and which config file it came from) rather than
    probed and silently mis-recorded. Pin detection covers every package
    manager the tool can update, not just npm: a `:tag`-pinned docker image
    and a `cargo install --version` pin are refused the same way. Docker
    needed its own handling because package detection strips the tag off the
    image reference, so `docker run acme/server:1.2.3` would otherwise pull
    `acme/server:latest`, restart the still-pinned config, and record the new
    digest as active.

## [2.1.0] - 2026-08-12

### Added
- **`allowPrivateRegistry` config field.** The private-registry opt-in was
  env-var-only (`PMCP_REGISTRY_ALLOW_PRIVATE`), which roadmap v9's PRIVREG
  criterion specified as "env var + config field"; only the env var was ever
  built. It is now also a top-level boolean in the config file, beside
  `autoStart`, resolved project > user > custom. The env var still wins, but
  **only when explicitly set** -- an unset variable is not a preference, so it
  cannot silently override a config file that enabled the flag. Non-boolean
  values are ignored with a warning rather than coerced, so a truthy string
  cannot quietly enable a security-relevant opt-in. Default remains OFF.
  (Consiliency/pmcp#139)
- **`telnyx` manifest entry.** Telnyx now publishes an official MCP server
  (`telnyx-mcp`, maintained by `team-telnyx`, from
  `github.com/team-telnyx/telnyx-node`); the entry was previously deferred
  because the package name then tracked the Telnyx SDK version line. Note its
  primary tool is `execute`, which runs TypeScript against a pre-authenticated
  SDK client -- a code-execution surface rather than a narrow API wrapper, and
  classified HIGH risk accordingly. (Consiliency/pmcp#77, partial: Algolia
  still has no official server and remains deferred.)

## [2.0.1] - 2026-08-11

### Fixed
- `gateway.describe` no longer claims a technical failure on success. The
  feedback hint it emitted opens with "Technical failure detected", and it
  was attached unconditionally, so every *successful* describe told the
  client something had gone wrong. `invoke`'s success path already passed
  none; this site did not follow that convention. All 25 hint call sites
  were audited -- the other 24 are genuine failure paths and are unchanged.
- Tool risk classification now honours the server's own MCP
  `ToolAnnotations`. A tool declaring `readOnlyHint` / `destructiveHint`
  states this authoritatively; PMCP was overriding that with a keyword
  guess. The fallback heuristic also matched risk words as *substrings*
  against the whole description, so context7's read-only
  `resolve-library-id` was classified high-risk and reported as one that
  "may modify data" -- because its description says "Source Reputation",
  and "reputation" contains "put". The fallback now matches on word
  boundaries. Risk is advisory (display and `catalog_search` filtering);
  policy does not gate on it, so this is not a security change.
- A gateway with no downstream server connected no longer tells clients it
  has no tools. Downstream servers are lazy unless listed in `autoStart`,
  so "MCP Gateway: No tools currently available" was what a fresh gateway
  sent *every* client on connect, while its own ~26 meta-tools were
  available and were the route to everything else. The message now says no
  downstream server is connected yet, that this is normal, and names the
  tools that do work. Both the cached and template paths share one helper
  so the first impression cannot differ by cache state.
- Remote downstream transports (SSE, streamable HTTP) are now entered and
  closed in a dedicated per-client owner task, fixing an anyio cancel-scope
  task-ownership violation during teardown: the exit stack used to be
  entered by the task that connected and closed by whichever task called
  `disconnect_server` / `disconnect_all` / a reconnect, which anyio's
  cancel scopes don't allow. This did not leak resources (the owned
  `httpx2.AsyncClient` always closed), but the violation itself was real
  and, in the one path where the entering task was still alive, surfaced as
  a `CancelledError` escaping `disconnect_all()`.

## [2.0.0] - 2026-08-09

### Removed
- **`GET /mcp` is retired.** It now answers `405 Method Not Allowed` with
  `Allow: POST, DELETE` instead of accepting a standing connection. The
  rmcp-compat pre-session keep-alive SSE workaround that used to answer that
  GET is gone with it, and so are the two env vars that configured it,
  `PMCP_MAX_KEEPALIVE_STREAMS` and `PMCP_KEEPALIVE_MAX_SECONDS`. `GET
  /health` and `GET /metrics` are untouched by this and continue to work
  exactly as before.
  The concurrency cap the old keep-alive enforced returns one-for-one as
  `PMCP_MAX_LISTEN_STREAMS` (same default, 64), bounding the new
  `subscriptions/listen` stream instead. **The absolute-lifetime cap
  (`PMCP_KEEPALIVE_MAX_SECONDS`) is deliberately not replaced** — a
  subscription is long-lived by design, and a server that silently severs it
  every N seconds is the defect this release fixes, not a property to
  preserve. If you relied on that env var, there is no replacement for it;
  what now bounds exposure is the concurrency cap, the SDK's own per-stream
  event-backlog cap, and `/mcp` auth whenever `auth_mode` is configured.
  The replacement for the retired GET stream is `subscriptions/listen`
  (`notifications/tools/list_changed`, `notifications/resources/list_changed`
  and `notifications/prompts/list_changed`, delivered on an open listen
  stream), reachable only at protocol version `2026-07-28`. No existing
  client loses delivered data from this change — pmcp never published
  anything on the old GET stream, so retiring it removes a channel pmcp
  never wrote to, not one clients were receiving events over.

### Changed
- **The declared `mcp` bound is raised from `>=1.8.0,<2.0.0` to `>=2.0.0,<3.0.0`,
  and the gateway's upstream server and downstream client are ported onto it.**
  pmcp now serves two protocol eras simultaneously, each reached a different
  way: the handshake era — `2024-11-05` through `2025-11-25` — is negotiated
  the way it always was, through `initialize`; the modern era — `2026-07-28`
  — is not negotiable through `initialize` at all, and is instead selected
  per request by the `MCP-Protocol-Version` header plus a `params._meta`
  envelope (`io.modelcontextprotocol/protocolVersion` and
  `io.modelcontextprotocol/clientCapabilities`), with `server/discover`
  advertising it out of band. Downstream, pmcp continues to speak only the
  handshake era to the servers it proxies — `client/manager.py`'s
  `PREFERRED_PROTOCOL_VERSION` ceiling is unchanged at `2025-11-25` — so no
  downstream server is reached at `2026-07-28` by this or any prior release.
  One limitation of the modern era is structural, not a pmcp gap: it has no
  back-channel, so server-initiated requests such as `sampling/createMessage`
  and `elicitation/create` do not exist at `2026-07-28` (pmcp does not proxy
  either today, so this changes nothing pmcp currently does — it is recorded
  here because a future phase that wants to add one will hit it).
  Declares `httpx`, `httpx2`, and `jsonschema` as direct dependencies for the
  first time — `mcp` 2.0.0 depends on `httpx2` rather than `httpx`, and no
  longer validates tool input schemas itself — closing the undeclared-
  transitive-dependency gap that has shipped this repo broken before.

## [1.22.0] - 2026-08-06

### Added
- **Manifest credential optionality (`api_key_optional_when`).** A manifest
  server entry can now name the `extra_env` variable (e.g. a self-hosted base
  URL) whose presence makes its declared credential unnecessary, so reaching
  a self-hosted endpoint no longer requires planting a placeholder secret like
  `FIRECRAWL_API_KEY=self-hosted-no-auth` — which previously read as a real
  credential to whoever found it. The field alone changes nothing: an operator
  must separately supply the named variable via `extra_env` or a `server_env`
  overlay patch before the credential relaxes, and a server cannot name its
  own credential as its relaxer. Every one of the seven places that gate on
  `requires_api_key` — eager startup, install/provision preflight, lifecycle
  connect, provisioning, `pmcp secrets check`, capability discovery
  (`gateway.catalog_search`/`gateway.request_capability`), and `pmcp init` —
  now reads the effective requirement through one shared predicate, and every
  unset, malformed, self-referencing, or placeholder (`${VAR}`) relaxer value
  fails closed. Ships `firecrawl`'s `api_key_optional_when: ["FIRECRAWL_API_URL"]`
  as the first entry using it — inert on its own for every existing install.
  (Consiliency/pmcp#114)

### Changed
- **The declared `mcp` floor was corrected from `>=1.0.0` to `>=1.8.0`, because
  `>=1.0.0` was never installable.** `client/manager.py` imports
  `streamablehttp_client` from `mcp.client.streamable_http` at module scope, and
  that module first exists in `mcp` 1.8.0 — pinning 1.0.0, 1.6.0, 1.7.0, or
  1.7.1 fails the gateway's startup import with
  `ModuleNotFoundError: No module named 'mcp.client.streamable_http'`. The bound
  was set by installing at each candidate, not by reading source. The upper
  bound is unchanged; supporting `mcp` 2.x is still tracked separately.
- **CI now proves the declared minimum actually works.** `install-smoke` only
  ever exercised the *ceiling* — a fresh resolve always picks the newest allowed
  version, so a bogus lower bound is invisible to it. A new `min-version-smoke`
  job parses the floor out of `pyproject.toml`, installs the built wheel pinned
  at exactly that version, imports the gateway's startup modules, then boots the
  gateway and drives a real tool call through it to a throwaway downstream
  server. Imports alone are not acceptance for a gateway, and neither is
  `/health` — it returns a hardcoded literal. Only the round trip exercises
  session initialization, tool discovery, and invocation, which is where an
  `mcp` break actually lands.
- **The test workflow now runs on a schedule and on demand.** A weekly
  `schedule:` (`0 8 * * 1`) plus `workflow_dispatch:` join the existing push and
  pull-request triggers. 1.21.0 could have broken with zero commits to this
  repo: `mcp` 2.0.0 was published after the last CI run and nothing re-ran to
  notice. Push and PR triggers cannot catch a dependency that moves underneath
  an already-released package.
- **A drift test pins `pmcp.__version__` to the installed distribution's
  metadata.** `tests/test_package_metadata.py` fails when the version in
  `pyproject.toml` and the one in `src/pmcp/__init__.py` disagree. A release
  that bumps one without the other makes everything reading `pmcp.__version__` —
  the `/health` payload and the `pmcp/{version}` User-Agent among them — report
  the wrong release.

### Fixed
- **Credential-gate startup tests no longer depend on the machine's manifest
  overlays.** `tests/test_credential_gates_startup.py` passed in CI but failed
  for any operator with a real `~/.pmcp/manifest.yaml` supplying a relaxer
  variable: the relaxer legitimately fired, so `pmcp init` printed
  "No credential needed" where the test expected the credential instruction.
  Patching `HOME` is not sufficient to isolate manifest resolution — it blocks
  only the user-overlay lookup, not the independent project-overlay walk from
  `Path.cwd()`, and `PMCP_MANIFEST_PATH` is the top of the overlay chain rather
  than a bypass. These tests now pin an explicit manifest path. The direction
  that mattered was the inverse of the observed failure: a test asserting the
  *relaxed* path would have passed for the wrong reason — green because the
  operator's overlay supplied the relaxer, not because the code worked.
  (Consiliency/pmcp#125)
- **The last outstanding item from `specs/phase-plans-v10.md` Phase 6 is closed
  out.** `test_monitor_server_ready_on_startup_pattern`
  (`tests/test_manifest.py`) asserted `job.status == "server_ready"` after a
  fixed `await asyncio.sleep(0.3)`, racing the install monitor's pattern match
  against the wall clock. It now `await`s `job._monitor_task` directly — the
  monitor task returns as soon as it detects the startup pattern, so this is a
  deterministic, event-driven wait with no timing dependency, matching the
  idiom its sibling tests in the same class already use.

## [1.21.1] - 2026-08-01

### Fixed
- **A fresh install was dead on arrival.** The `mcp` dependency was declared as
  `mcp>=1.0.0` with no upper bound. `mcp` 2.0.0 renamed
  `mcp.client.streamable_http.streamablehttp_client` to
  `streamable_http_client`, which `client/manager.py` imports at module scope,
  so any clean `pip install pmcp` / `uv tool install pmcp` resolved to 2.x and
  the gateway died at startup with
  `ImportError: cannot import name 'streamablehttp_client'`. Existing installs
  and development checkouts were unaffected — `uv.lock` pins a 1.x `mcp`, which
  is precisely why the entire test suite passed while the published artifact was
  broken. Now capped at `mcp>=1.0.0,<2.0.0`. Supporting `mcp` 2.x is tracked
  separately; raising the cap requires porting that import and auditing the rest
  of the 2.x surface.

### Changed
- **CI now installs the built wheel with dependencies resolved from scratch.**
  Every existing job installs via `uv sync`, which uses `uv.lock`, so nothing
  ever exercised the version constraints an end user actually resolves against.
  A new `install-smoke` job builds the wheel, installs it into a clean
  environment with no lockfile, prints the resolved versions, and imports the
  modules a real startup touches. Verified to fail on the previous unbounded
  constraint and pass on the capped one — this class of break can no longer ship
  green.

## [1.21.0] - 2026-08-01

### Added
- **Non-secret server environment variables (`extra_env`).** A manifest server
  entry can now declare environment variables beyond its single credential —
  typically a base URL selecting a self-hosted deployment. Previously the only
  way to reach a non-default endpoint was to start the whole gateway with the
  variable pre-exported, which is process-global and invisible to the manifest.
  Values are non-secret by design; credentials stay in `env_var`/`secret_key`
  and always win over a colliding `extra_env` key. Applied on **every** server
  spawn path — install-and-run, restart, refresh, lazy reconnect, and lifecycle
  connect — so a self-hosted endpoint survives a gateway restart rather than
  silently reverting to the vendor default. A configured `.mcp.json` entry that
  duplicates a manifest server inherits it too, with any value that config sets
  explicitly winning as a genuine user override. (#108, #109)
- **Per-host overlay patching (`server_env`).** A private overlay
  (`~/.pmcp/manifest.yaml`, `<project>/.pmcp/manifest.yaml`,
  `$PMCP_MANIFEST_PATH`) can patch `extra_env` on an existing server without
  redeclaring the whole entry, so pointing a shipped server at a self-hosted
  endpoint no longer means hand-copying its command, args, and install block —
  a copy that would silently shadow later upstream fixes. `servers:` keeps its
  whole-entry-replace semantics unchanged; a patch naming an unknown server
  warns and is skipped rather than creating one. (#105)
- **`blacksmith` CLI alternative.** Agents now discover the Blacksmith CLI
  (Testbox, job history, log search, CI usage) instead of looking for an MCP
  server — Blacksmith publishes none, and its CLI is the supported agent
  surface. (#106, #107)

## [1.20.0] - 2026-07-26

### Security
- Add the fail-closed `scoped_advisor_audit.v1` profile for isolated advisor
  research. Explicit policy failures now terminate startup, gateway-owned tools
  are filtered in discovery and dispatch, and the shipped profile exposes only
  health, catalog search, describe, and invoke controls over approved Firecrawl
  and Bright Data research patterns.
- Add typed run/seat/evidence correlations and an explicit JSONL audit sink with
  privacy-safe source/result digests, contiguous sequence/count validation, and
  exactly one fsynced terminal marker. Raw URLs, queries, arguments,
  credentials, and result bodies are not written to the audit. (Consiliency/pmcp#103)

### Added
- `pmcp capabilities --json`, `--audit-jsonl`, and `PMCP_AUDIT_JSONL` expose the
  released feature contract required by Consiliency/agent-harness#310.

## [1.19.4] - 2026-07-18

### Changed
- **Packaging metadata points at the new owning org.** The project URLs
  (`Repository`/`Homepage`/`Issues`) now target `Consiliency/pmcp` following the
  GitHub repository transfer from `ViperJuice/pmcp` (the old URLs still redirect,
  but the PyPI project page linked to the stale location). No functional or API
  changes — this is a metadata-only patch. It also serves as the first release
  published from the new org, validating the trusted-publishing path end-to-end.
  (#100 follow-up)

## [1.19.3] - 2026-07-12

### Security
- **No cross-server credential bleed.** The gateway loads every PMCP-stored
  credential into its own `os.environ`, and the subprocess-spawn paths copied
  that whole environment — so each downstream MCP server received *every other*
  server's credentials (e.g. `@brightdata/mcp` got the OpenAI key). Downstream
  subprocesses now inherit the gateway environment **minus** PMCP-managed secret
  keys (the keys in the user `pmcp.env` + project `.env.pmcp` stores), then get
  only their own resolved credential re-applied. This covers both the server
  spawn (`_connect_stdio`) and the paths that execute a server's **package code**
  (`start_install`/`install_server`, and the `update_server` / `verify_installation`
  `--help` probes). Non-secret ambient vars (`PATH`/`HOME`/`NODE_*`/proxy/locale)
  are preserved. (#96)

  Scope: only PMCP-managed secrets are stripped — not secrets the operator
  exported into the shell or a plain `.env`. Behavior change: a server that
  relied on *ambient inheritance* of a secret it does not declare (no config
  `env` block, no manifest credential) no longer receives it. Follow-ups tracked
  in #96: sanitizing the trusted-CLI probes as defense-in-depth, and the durable
  fix of not loading secrets into the global `os.environ` at all.

## [1.19.2] - 2026-07-11

### Fixed
- **Per-server credential storage-key namespacing.** A server can now store its
  credential under a namespaced key (manifest `secret_key`, e.g. Bright Data's
  `BRIGHTDATA_API_TOKEN`) while the downstream process still receives the generic
  runtime `env_var` it reads (`API_TOKEN`). This removes the collision risk of
  two servers sharing a generic name like `API_TOKEN` in the flat secret store.
  Every credential path resolves through `credential_lookup_keys` (namespaced key
  first, legacy `env_var` as a backward-compatible fallback — no migration
  required): the `auth_connect` write path, the provision auth gate
  (`check_api_key`), manifest/configured/eager/lazy/refresh startup-config
  resolution, the install-and-run subprocess environment (adopted `npx` servers),
  and the connect/describe availability checks. Configured `.mcp.json` entries
  that match a manifest api-key server inherit the resolved credential (including
  dead `${VAR}`/`$VAR` placeholders that local stdio env never expands), and the
  `pmcp secrets check` / `pmcp init` diagnostics are namespace-aware. (#95)

## [1.19.1] - 2026-07-06

### Fixed
- **`gateway.describe` now exposes nested array/object item schemas.** Array and
  object arguments were collapsed to a bare `"array"`/`"object"` type, hiding
  item shape and required item fields, so agents wasted calls guessing at the
  payload (e.g. `brightdata::search_engine_batch` needs
  `{"queries": [{"query": "..."}]}`). `ArgInfo` gains an optional `item_schema`
  with a compact one-level summary, and the `invoke_template` placeholder now
  shows the nested shape (e.g. `[{"query": "<string>"}]`). Scalar-arg output is
  unchanged. (issue #87)
- **`index-it-mcp` manifest entry now launches the `stdio` transport.** The
  shipped entry ran `serve` (the HTTP/admin surface) where PMCP's local process
  path needs a stdio MCP child; `command`/`args` and all four install platforms
  now end in `stdio`. (issue #89)

### Docs
- README pilot config for provisioning `index-it-mcp` via `.mcp.json` with a
  pinned version and the operational env block, noting that per-entry `env` is
  honored only from `.mcp.json` (the manifest/overlay schema has no `env:`).

## [1.19.0] - 2026-07-04

Remediation of the v1.18.0 code review (roadmap `specs/phase-plans-v10.md`).

### Fixed
- **Downstream servers now actually recover from failure.** A crashed stdio
  server never came back: `_cleanup_client` cancelled the very connect task
  driving the reconnect (a `gather()` cancelling itself → `RecursionError`).
  `_cancel_background_tasks` now excludes the current task and `_cleanup_client`
  cancels only the client's own read/stderr tasks. Remote (SSE/HTTP) servers now
  auto-reconnect on an unexpected drop (parity with stdio). Failed connects no
  longer leave a stale ERROR client or a leaked stderr reader task. Proven by a
  real-subprocess integration test.
- Transport DoS bounds: pre-session keepalive SSE streams are capped
  (`PMCP_MAX_KEEPALIVE_STREAMS`, default 64) with an absolute lifetime
  (`PMCP_KEEPALIVE_MAX_SECONDS`, default 300); POST bodies are enforced against
  the size cap **during read** so chunked/unadvertised-length bodies can't bypass
  it (413), with a request timeout closing slow-trickle connections.
- Robustness: terminal task records are evicted past a cap (was unbounded);
  `disconnect_server` re-cancels pending requests under the lifecycle lock
  (orphaned-future window); malformed `.mcp.json` is surfaced (path + reason)
  instead of silently disabling its servers; `find_project_root` no longer
  resolves to `$HOME`; version-check URLs are `quote()`-escaped; the two flaky
  monitor tests are now deterministic.

### Security
- **OAuth 2.1 resource-server auth mode and Origin/DNS-rebinding validation are
  now reachable.** They were fully implemented and tested but never passed to the
  running server. New CLI flags/env (`--auth-mode`, `--oauth-issuer`,
  `--oauth-jwks-url`, `--oauth-audience`, `--required-scope`, `--allowed-origin`;
  `PMCP_AUTH_MODE`/`PMCP_OAUTH_*`/`PMCP_ALLOWED_ORIGINS`) thread them through
  `GatewayServer`. The Origin check now runs by default: a cross-origin browser
  `Origin` is rejected (loopback/same-origin/allowlisted pass; no-Origin clients
  unaffected). Host validation is opt-in when origins are configured.
- Provisioning input validation: a discovered/registered package name is now
  validated (rejects leading-dash flag injection, path separators, shell/URL
  metachars) before it can reach `npx -y <name>`, and the resolved install
  command is echoed for confirmation. `auth_connect` refuses to persist/export an
  env var that isn't the target server's declared credential variable (hard
  denylist for `LD_*`/`DYLD_*`/`NODE_OPTIONS`/`PATH`/`PYTHON*`), closing an
  `LD_PRELOAD`-into-subprocess code-exec path.
- Outbound-fetch hardening: JWKS and auth-metadata fetches no longer follow
  redirects to internal hosts; the registry response is size-capped during a
  streamed read (no OOM); the private registry endpoint requires `https` and
  rejects link-local/metadata hosts; the registry cache is written atomically at
  `0600`.
- Local credentials: `pmcp secrets set` reads the value via prompt/`--stdin`
  (never argv/shell history); the secret directory is created `0700`;
  `submit_feedback` scrubs the title and all fields (not just the description)
  before posting to a public issue.

### Changed
- Policy allow/deny matching is now **case-sensitive** (matching server-ID
  semantics elsewhere in the gateway).
- `pmcp status` no longer eagerly connects every server as a side effect of a
  read-only query — it reports the lazy view by default; use `--probe` to connect.
- Explicit `--max-concurrent-spawns` / `--rate-limit` / `--request-timeout` flags
  now take precedence over the matching env var (previously a flag equal to the
  default was silently overridden).

## [1.18.0] - 2026-06-29

### Added
- Private/custom manifest overlay: define your own provisionable MCP servers in
  `~/.pmcp/manifest.yaml` (user), `<project>/.pmcp/manifest.yaml` (project), or a
  file pointed to by `PMCP_MANIFEST_PATH`, merged over the shipped manifest
  without editing it. Precedence is shipped < user < project < `PMCP_MANIFEST_PATH`
  (same-named entries are replaced whole). Overlay parsing is fail-soft — a
  missing file is skipped, and a malformed file or a single bad entry logs a
  warning and is skipped without crashing the gateway. Merged servers
  participate identically in `gateway.request_capability`, `gateway.catalog_search`,
  `gateway.provision`, and startup `refresh` resolution, gated by the same
  policy/auth checks.

## [1.17.1] - 2026-06-29

### Fixed
- Native Windows gateway startup no longer crashes with `No module named 'fcntl'`
  (issue #84). `acquire_singleton_lock`/`release_singleton_lock` imported the
  Unix-only `fcntl` unconditionally; they now take the per-user single-instance
  lock via `msvcrt.locking` on Windows and `fcntl.flock` on POSIX (selected by a
  literal `sys.platform` check so type checkers narrow the platform-only
  imports). WSL is unaffected. (`resource` was already import-guarded.)

## [1.17.0] - 2026-06-28

### Fixed
- A single downstream stdout line larger than the read limit (default 10 MiB,
  `PMCP_STDIO_READ_LIMIT`) no longer disconnects the whole server (issue #79,
  symptom 1b). The stdout reader now reads in chunks and splits on newlines
  itself: an oversized line is dropped — failing only the request it belongs to,
  with an actionable "output too large" message — while the connection and other
  pending requests stay alive, so the *next* call no longer fails. Large browser
  responses (full-page snapshots, screenshots) were a common trigger of the
  reported "session expired"/instability. A reproduction harness lives in
  `diagnostics/issue-79-1b/`.
- Downstream tool calls are no longer killed by a fixed wall-clock deadline
  (issue #79, symptom 1a). `timeout_ms` is now an **inactivity (idle) timeout**:
  a call survives as long as the downstream MCP server keeps producing output
  (including JSON progress notifications, which now count toward per-request
  liveness in both the stdio and SSE readers). An absolute backstop caps total
  wall-clock time so a chatty-but-never-completing call cannot hang forever —
  configurable via `PMCP_REQUEST_CEILING_MS` (default 600000ms / 10 min). The
  long ceiling applies only to tool invocations; control-plane requests
  (initialize, list calls) keep the tighter idle deadline so one stuck server
  can't stall startup/refresh. This unblocks legitimately long browser/
  automation operations driven through the gateway.
- Stdio servers are now spawned in their own session/process group
  (`start_new_session=True`) and reaped as a whole tree on disconnect (issue
  #79, symptom 1c). Previously, killing a stdio server (e.g. `@playwright/mcp`)
  left the browser it launched orphaned to init, holding the profile's
  SingletonLock and breaking the next launch. A new group-aware
  `_terminate_process_tree` helper (SIGTERM the group, wait, then SIGKILL, with
  a single-process fallback when the process is not a group leader, or on
  Windows where process groups are unavailable) now backs all four downstream
  shutdown paths.
- `gateway.refresh` is now diff-based and non-destructive (issue #79, symptom
  2). Servers whose resolved config is unchanged are left connected and
  running; only servers that were removed or whose config changed are
  disconnected, and only newly-added eager servers are connected. Previously
  refresh tore down **every** server and reconnected only the eager set, which
  dropped previously-running lazy/provisioned servers to offline (the reported
  "105 seen, 0 online") and needlessly respawned unchanged processes (e.g. a
  live browser) on every refresh. The diff keeps only servers that are actually
  ONLINE — a crashed eager server is reconnected (recovery path preserved);
  reconciles the lazy registry to the resolved keep-set so removed/policy-denied
  servers can no longer be lazily started; and reconnects a remote server when
  its `${VAR}` auth token has rotated in the env store (compared against the
  connect-time resolved headers), instead of keeping stale/revoked auth.
- Process-tree reaping now escalates to a group `SIGKILL` when the leader exits
  but a grandchild (e.g. a `SIGTERM`-ignoring browser) survives the `SIGTERM`
  grace period, and reaps servers concurrently at shutdown so multiple hung
  servers can't exceed the shutdown budget and orphan browsers (issue #79/1c).

### Added
- `gateway.catalog_search` with `include_offline=true` now surfaces
  manifest-only provisionable servers in a dedicated `manifest_candidates` field
  when the query matches a manifest server by name or keyword but no cached
  tools exist yet (issue #78). Candidates carry machine-readable next-action
  metadata (`provisionable`, `provision_tool`, `request_capability_tool`,
  `auth_tool`, `requires_api_key`, `api_key_available`, `env_var`) so an agent
  can provision the exact server instead of falling back to a plain web search.
- Manifest: `brightdata` keywords extended with `web research`, `current web`,
  `page fetch`, and `external research` so research-oriented prompts match
  without the exact brand name.

### Changed
- `gateway.request_capability` tool description now states it **recommends** a
  server to provision (and that `gateway.provision` does the actual install/
  start), matching the implementation — it previously claimed to auto-provision
  (issue #78).

## [1.16.0] - 2026-06-25

### Added
- Manifest: 2026 vendor-official servers — Okta (identity/IAM), Zapier
  (automation meta-connector), Shopify Dev and Square (commerce), Snowflake
  Cortex (data platform), Pinecone (vector DB), Azure DevOps, and the GitLab Duo
  remote MCP. Adds Identity, Automation, E-commerce, and Data-Platform discovery
  coverage.
- Manifest (Tier 2): Elasticsearch, Chroma, Redis, Databricks (managed remote),
  Storybook, and Cloudinary — rounding out search/vector, data-platform,
  frontend, and media coverage.

### Changed
- Manifest: `mongodb` now points at the official `mongodb-mcp-server`
  (mongodb-js) instead of the community `mongodb-lens`, covering Atlas admin in
  addition to queries.

## [1.15.0] - 2026-06-25

### Added
- `SPEC_COMPLIANCE.md` tracks PMCP against the current stable MCP revision
  (`2025-11-25`) with a per-requirement compliance table, a draft-revision
  migration assessment (stateless transport, the `io.modelcontextprotocol/tasks`
  extension, `server/discover`, `CacheableResult`, DCR→Client ID Metadata
  Documents, SSE resumability), and a next-stable tracking checklist. PMCP is
  confirmed compliant with the folded-in current-stable items (403 on invalid
  `Origin` per PR #1439, `insufficient_scope` 403 step-up per SEP-835,
  input-validation as tool-execution errors per SEP-1303, JSON Schema 2020-12
  default dialect per SEP-1613, tool/resource/prompt icon passthrough per
  SEP-973).
- MCP Registry incremental sync. The registry client persists `last_synced_at`
  and accepts `updated_since` to fetch only changed servers
  (`?version=latest&updated_since=…` with cursor pagination), merging deltas into
  the cache via `merge_registry_delta`. A failed incremental attempt degrades to
  a full fetch, and a failed full fetch degrades to the prior cache. Default
  full-fetch callers are unchanged.
- Opt-in private-registry support (`PMCP_REGISTRY_ALLOW_PRIVATE`, default off).
  When enabled with `PMCP_REGISTRY_PRIVATE_ENDPOINT`, PMCP discovers from a
  private/custom registry and tolerates draft/non-GA `server.json` schema fields,
  surfacing all versions — a debugging aid for developers building their own
  private MCP servers, not for production discovery. With the flag off,
  discovery behavior is unchanged.

## [1.14.2] - 2026-06-24

### Removed
- Removed the unused BAML/LLM machinery entirely. Outbound LLM calls were retired
  long ago in favor of a pure-Python capability router, leaving the `baml-py`
  dependency, the generated `baml_client`, `baml_src/`, the retired
  `llm_summarizer` stub, and the dead `use_llm` / `use_llm_fallback` code paths as
  vestigial. Dropping them removes a heavy dependency (no more native binary) and
  the per-release client-regeneration burden. Capability summaries and code
  snippets are unchanged: cache → template for summaries, static templates for
  snippets. The `llm` optional-dependency extra is gone.

## [1.14.1] - 2026-06-23

### Fixed
- Regenerated the vendored `baml_client` against the pinned `baml-py` (0.222.0).
  The client had been generated for 0.219.0 and raised a version-incompatibility
  error under the shipped runtime, so the LLM capability summarizer silently fell
  back to template summaries since 1.13.x. It now loads BAML correctly, and guard
  tests assert the generated client matches the installed `baml-py` so the drift
  cannot recur unnoticed.

### Changed
- Bumped `baml-py` to 0.222.0 and CI `actions/checkout` to v7 (dependabot #63, #75).

## [1.14.0] - 2026-06-15

### Added
- Tenant code-mode host integration (v7 HOSTSOAK). PMCP brokers discovery,
  invocation, downstream task lifecycle, policy, redaction, and operator
  guidance for a companion tenant code-mode MCP server, but does not run
  scripts itself.
- OAuth 2.1 Resource Server auth mode (opt-in, fail-closed). When configured,
  PMCP validates access-token signatures against an async, TTL-cached JWKS and
  binds the `aud` claim to an explicitly-configured canonical resource URI
  (RFC 8707). The legacy static bearer remains the default single-tenant mode.
- MCP Registry-backed discovery metadata with a deterministic local cache.
  `gateway.request_capability`, `gateway.catalog_search`, and
  `gateway.search_registry` surface registry candidates — including remote
  (streamable-http/sse) servers — with transport, package, and remote auth
  metadata, while preserving the explicit `gateway.register_discovered_server`
  and `gateway.provision` boundary.
- Curated registry-backed vendor entries for GitHub, Atlassian Rovo,
  Cloudflare, Sentry, Vercel, and Hugging Face using placeholder header names
  only.

### Fixed
- Secret redaction now covers every task-emitting gateway surface. `gateway.invoke`,
  `gateway.tasks_result`, `gateway.tasks_list`, and `gateway.tasks_get` route
  returned task `status_message`/`raw` through the canonical redactor; truncation
  summaries are built from post-redaction text; bare `sk-`/`ghp_`/`github_pat_`
  tokens are redacted; and redaction is decoupled from the 400-char diagnostic cap
  so large results are no longer silently truncated. Redaction defaults on for
  task/code-mode results.
- Shared-gateway concurrency hardening. The downstream connect/reconnect paths now
  acquire the lifecycle lock (no longer racing `refresh`/`disconnect` into orphaned
  subprocesses or a torn tool catalog), background tasks are tracked and cancelled
  on teardown, request IDs carry a per-connection epoch so a stale `gateway.cancel`
  cannot hit a new request, and the lock is no longer held across reconnect backoff.
- Resource-server auth no longer derives the token audience from the client Host
  header, fetches JWKS via a blocking call on the event loop, or returns HTTP 500
  for JWKS/algorithm errors; algorithms are restricted to an operator allowlist and
  `jwks_url` must be https on a public host.
- Capability matcher scoring no longer drops common queries (`database sql`,
  `headless browser`, `postgres database`) below the match threshold against the
  shipped manifest.
- Registry client now models remote servers, deduplicates to the latest version,
  paginates, bounds the response size, fetches asynchronously, and uses a stable
  cache path; project-scope credential writes and downstream header reads resolve
  the same project root.

## [1.13.1] - 2026-05-06

### Fixed
- Stdio downstream MCP servers no longer surface a misleading "disconnected
  unexpectedly" warning when a tool response exceeds asyncio's default 64 KiB
  per-line read limit. PMCP now spawns child processes with a 10 MiB stdout
  read limit (overridable via `PMCP_STDIO_READ_LIMIT`) so realistic responses
  from `brightdata::scrape_batch`, large `playwright` screenshots/DOM dumps,
  `fetch` on long pages, and similar tools are returned to the caller intact
  instead of triggering a phantom reconnect cycle.
- The stdout reader now distinguishes `LimitOverrunError` and other read
  failures from normal process exit, logs the cause at WARNING, and propagates
  it into `ServerStatus.last_error` so `gateway.health` and `pmcp status`
  report the actual failure mode instead of "Server process exited".

## [1.13.0] - 2026-04-23

### Added
- PMCP discovery now exposes compact native CLI guidance for installed CLIs
  through `gateway.request_capability` (`status="use_cli"`) and
  `gateway.catalog_search` (`cli_hints`) so the model can switch to
  Bash/direct CLI without a second PMCP discovery step.

### Changed
- Clarified the CLI-first discovery contract in docs and tests: PMCP provides
  help commands and curated examples, does not execute the recommended shell
  command, and does not add a general `pmcp invoke` transport for native CLIs.

## [1.12.0] - 2026-04-23

### Added
- Added an offline AUTHSOAK release-gate matrix for local API-key auth,
  remote bearer-header placeholders, remote auth challenges, insufficient
  scopes, URL-mode elicitation, malicious auth URLs, and non-secret
  status/doctor/feedback evidence.

### Changed
- Tightened operator auth documentation for env-store scope selection, remote
  header placeholders, URL-mode non-goals, redaction limits, and HTTP endpoint
  exposure expectations.

### Fixed
- Redacted `bearer=` query parameter values anywhere auth URLs are sanitized or
  rendered in diagnostics.

## [1.11.0] - 2026-04-22

### Added
- Downstream MCP initialization now prefers protocol version `2025-11-25`,
  records negotiated protocol versions and server capabilities, and preserves
  compatibility with older supported protocol versions.
- Tool, resource, and prompt indexing now preserves modern MCP metadata
  additively, including titles, icons, output schemas, annotations,
  execution/task support hints, unknown raw metadata, and JSON Schema dialects.
- `gateway.invoke` can request downstream MCP task-augmented execution for
  task-capable tools, and required-task tools are routed through task metadata
  automatically.
- Added `gateway.tasks_list`, `gateway.tasks_get`, `gateway.tasks_result`, and
  `gateway.tasks_cancel` for gateway-safe downstream MCP task brokering.
- Added structured downstream auth state reporting for missing auth,
  insufficient scope, policy denial, and URL-mode elicitation, with safe
  authorization metadata discovery hints.
- Added additive gateway observability models for trace context, bounded
  structured audit events, and gateway transport diagnostics.
- `gateway.health` can now include safe `gateway_diagnostics` and recent
  redacted `audit_events`; `pmcp status --verbose` renders those diagnostics
  when a live gateway reports them.
- Streamable HTTP now reports safe `/health` transport diagnostics and tolerates
  `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, and trace context headers.
- Added CONFIG administration: `gateway.config_status`,
  `gateway.get_startup_policy`, and `gateway.set_startup_policy` expose
  source-attributed startup policy/status, preview-only default `autoStart`
  edits, explicit atomic apply, and non-secret stale/conflict diagnostics.
- `pmcp setup` now supports named profiles: `local-stdio`,
  `shared-local-http`, `authenticated-shared-http`, and `ci`.
- Registry and manifest discovery metadata can carry read-only package,
  server-card, capability, and diagnostic hints without changing provisioning
  semantics.

### Changed
- `gateway.catalog_search`, `gateway.describe`, `gateway.health`, and
  `pmcp status` can surface negotiated protocol and richer metadata without
  requiring older servers or clients to provide the new optional fields.
- Refresh, disconnect, and restart now account for active MCP tasks separately
  from PMCP pending requests and refuse active work by default.
- `gateway.auth_connect`, `pmcp status`, `pmcp doctor`, and HTTP 401 responses
  now share stricter redaction for bearer tokens, API keys, auth codes, URL
  userinfo, and sensitive query parameters.
- Tool/resource/prompt/server snapshots, pending requests, task lists, MCP
  server-facing lists, and catalog tie-breakers now use stable public ordering.

### Release Verification
- CONFORM release-gate coverage now exercises old-protocol fake payloads and
  current-protocol fake payloads across `2024-11-05`, `2025-03-26`,
  `2025-06-18`, and `2025-11-25` protocol responses.
- Local conformance tests cover modern tool/resource/prompt metadata
  preservation, task brokering, required-task capability refusal, structured
  auth and URL-mode elicitation states, trace context, audit events,
  startup-policy preview/apply behavior, and deterministic gateway/server
  ordering.
- Streamable HTTP smoke verifies `/mcp`, unauthenticated `/health` and
  `/metrics`, bearer auth, draft header tolerance, trace headers, rate-limit
  diagnostics, and existing rmcp/Codex compatibility paths with local
  Starlette/TestClient utilities only.
- Full release evidence for this gate passed locally: targeted conformance
  tests, whole phase regression, broader shared-service regression, full
  `pytest`, `ruff check`, `ruff format --check`, `mypy`, `uv build`, and local
  `pmcp status`, `pmcp doctor`, and `pmcp setup --profile ...` smoke commands.

## [1.10.0] - 2026-04-21

### Added
- `gateway.connect_server`, `gateway.disconnect_server`, and
  `gateway.restart_server` provide runtime-only lifecycle controls for known
  downstream servers with structured status output.
- `gateway.health` now includes optional startup policy fields for eager, lazy,
  skipped, policy-denied, missing-auth, and unknown `autoStart` decisions.
- `pmcp status --verbose` displays live startup policy details when available,
  including missing-auth environment variable names without exposing secret
  values.
- Startup and refresh logs now include concise policy summary counts and
  actionable messages for unknown `autoStart` and missing-auth skips.
- `pmcp doctor` now includes a named `http` check that probes gateway `/health`
  reachability without requiring bearer auth.
- Bounded multi-client soak coverage now exercises concurrent lazy invokes,
  same-server single-flight startup, refresh and lifecycle refusal/cancellation,
  health/list-pending/status visibility, and local HTTP shared-service smoke
  paths.

### Changed
- `gateway.refresh` now refuses by default while downstream requests are pending
  and reports pending-request counters. Passing `force=true` cancels those
  requests before refresh proceeds.
- `gateway.disconnect_server` and `gateway.restart_server` use the same
  target-server pending-request policy: refuse by default and cancel only that
  server's pending requests when `force=true`.
- Packaged manifest entries no longer mark Playwright or Context7 for automatic
  eager startup. Downstream servers remain lazy by default and can be eagerly
  started by adding their names to top-level `.mcp.json` `autoStart`.
- README and `pmcp setup` guidance now describe the user-owned startup model:
  `mcpServers` provides lazy availability, while `autoStart` opts selected
  servers into eager startup.
- README and SECURITY now document shared-service HTTP mode, per-source-IP
  `/mcp` rate-limit buckets, lifecycle disruption behavior, and unauthenticated
  `/health` and `/metrics` expectations.

### Migration
- To restore the previous eager startup behavior for the common browser/docs
  stack, add:
  ```json
  {
    "autoStart": ["playwright", "context7"],
    "mcpServers": {}
  }
  ```

## [1.9.2] - 2026-04-14

### Fixed
- **`gateway.request_capability` false-positive category matches** (closes #56):
  - Bug 1 — Generic keywords (e.g. "api") inflated category scores when multiple
    servers in one category each carried the same generic term. Replaced per-server
    frequency counting with category-span IDF weighting: a keyword appearing across
    N distinct categories gets weight 1.0 / 0.7 / 0.3 / 0.1 for N = 1 / 2 / 3 / 4+.
  - Bug 2 — Any non-zero score returned a category match. Added a minimum score
    threshold of 0.5 so pure generic-keyword overlap (e.g. three "api" hits × 0.1 = 0.3)
    falls through to `not_available` + `search_registry` guidance.
  - Bug 3 — Queries naming a specific unknown service (e.g. "Hostinger") still
    returned an unrelated category. Added a pre-check in `request_capability`:
    PascalCase words (non-first position) not matching any manifest server name
    cause Tier 2 to be skipped entirely, surfacing `not_available` immediately.

## [1.9.1] - 2026-04-13

### Added
- **`py.typed` marker** (`src/pmcp/py.typed`) — PEP 561 compliance; downstream
  projects using mypy/pyright now resolve PMCP types without `ignore_missing_imports`.
- **PyPI classifiers**: added `Operating System :: POSIX :: Linux`,
  `Operating System :: MacOS`, `Operating System :: Microsoft :: Windows`,
  and `Typing :: Typed`.
- **SECURITY.md**: documents threat model, known limitations, responsible disclosure
  process, and production hardening checklist.

### Fixed
- **Timing-safe auth token comparison**: replaced `!=` string equality with
  `hmac.compare_digest` to prevent timing oracle attacks on Bearer tokens.
- **Prometheus counter registration**: counters now registered at module import;
  fallback dict renderer kept in sync via `_inc()` helper so metrics are always
  visible in `generate_latest()` output.
- **Reconnect storm guard**: added `reconnecting: bool` flag to `ManagedClient`;
  prevents multiple concurrent `_reconnect_loop` tasks from spawning when a server
  exits rapidly.
- **HTTP request timeout**: tool invocations now wrapped in `asyncio.wait_for`
  (default 60 s, configurable via `--request-timeout` / `PMCP_REQUEST_TIMEOUT`);
  returns HTTP 504 on timeout.
- **Payload size limit**: `Content-Length > 10 MB` rejected with HTTP 413 before
  the body is read.
- **Windows signal handling**: `loop.add_signal_handler()` (POSIX-only) now
  guarded by `sys.platform != "win32"`; falls back to `signal.signal()`.
- CI mypy/ruff failures introduced by hardening changes.

## [1.9.0] - 2026-04-12

### Added
- **Production hardening**: authentication middleware, structured audit logging,
  sliding-window rate limiter (per-IP, configurable via env vars), and memory-leak
  fix for `_rl_store` cleanup.
- **Backstage catalog**: `catalog-info.yaml` and standard repo layout for
  Backstage/portal registration.
- **Consiliency maintenance trigger**: GitHub Actions workflow for scheduled
  maintenance worker.

### Fixed
- **rmcp/Codex HTTP transport compatibility** (closes #51): keep-alive SSE for
  session-less GETs; HTTP 202 for `notifications/initialized` without session ID;
  `_NullResponse` ASGI double-send guard.

## [1.8.1] - 2026-03-12

### Fixed
- README: corrected `pmcp setup` example to use `--mode http` (was `--mode sse`)
  and updated `pmcp doctor` comment to reflect HTTP transport.
- Test suite: resolved pre-existing failures (health isolation mock, subprocess
  PYTHONPATH, browser-invoke skip markers, ruff lint/format drift).
- Removed stale `TestBAMLSummarization` integration test (`generate_capability_summary`
  no longer makes outbound LLM calls since v1.8.0).

## [1.8.0] - 2026-03-11

### Changed
- **Transport**: Replaced deprecated SSE transport (`/sse`, `type: "sse"`) with MCP
  streamable-HTTP transport (`/mcp`, `type: "http"`). Eliminates the race condition
  where tool calls arrived before SSE session initialization completed.
  Update `~/.mcp.json`: `{"type":"http","url":"http://127.0.0.1:3344/mcp"}`
- **Capability routing**: Removed all outbound BAML/Groq LLM calls from
  `gateway.request_capability`. Replaced with three-tier pure-Python router:
  (1) sliding-window name match → single candidate, (2) category keyword match →
  all servers sorted by API-key availability, (3) not_available + search guidance.
  No API key required. New `pick_from_category` status added.
- `pmcp setup --mode sse` now generates `type: "http"` config (transport migration).

### Added
- **Background stale-version indexer**: pre-populates version check cache hourly so
  `catalog_search stale_updates` and `update_warning` fields are zero-latency.
- `stale_updates` field in `catalog_search` output listing servers with available updates.

### Fixed
- `fetch` manifest entry corrected: `@modelcontextprotocol/server-fetch` (404 on npm)
  → `uvx mcp-server-fetch` (PyPI).
- `pmcp doctor` and `pmcp setup` updated to probe/generate `/mcp` endpoint.

## [1.7.0] - 2026-03-08

### Added
- Background stale-version indexer task (see 1.8.0 above for details; released together).
- `stale_updates` field in `CatalogSearchOutput`.

### Changed
- Removed BAML outbound LLM calls from `request_capability` (see 1.8.0 above).

## [1.3.0] - 2025-01-23

### Added

- **Advanced LLM Features Documentation**: Comprehensive README section explaining optional Groq-powered capabilities
  - Semantic capability matching (vs keyword fallback)
  - LLM-generated tool summaries (vs static templates)
  - Dynamic code snippet generation
  - Step-by-step setup guide with Groq API key

- **Progressive Disclosure Integration Tests**: New test suite (`test_progressive_disclosure.py`)
  - Tests for all 8 workflow scenarios (Context7 + Playwright)
  - Coverage for search → describe → invoke workflow
  - Verification of naive prompt tool discovery

### Changed

- **Installation instructions**: Updated to prioritize `uv` as recommended package manager
- **baml-py dependency**: Updated to 0.215.2 for BAML compatibility

### Fixed

- BAML client version mismatch that prevented LLM features from working

## [1.1.0] - 2025-12-30

### Added

- **Code Execution Guidance System**: Multi-layered progressive disclosure to encourage models to use code patterns
  - **L0 (MCP Instructions)**: Brief philosophy about code execution (~30 tokens)
  - **L1 (Capability Cards)**: Ultra-terse code pattern hints during search (~8-12 tokens/card)
  - **L2 (Schema Cards)**: Optional code examples in tool details (~40-80 tokens/schema, opt-in)
  - **L3 (Methodology Resource)**: Full code execution guide (lazy-loaded via resource)

- **Guidance Configuration**: `~/.claude/gateway-guidance.yaml` for customization
  - Three levels: `off`, `minimal` (default), `standard`
  - Token budget estimation (~200 tokens in minimal mode)
  - Per-layer control for fine-grained configuration

- **Code Pattern Hints**: Keyword-based matching for common patterns
  - `loop` - For batch operations (navigate, create, update, list)
  - `filter` - For search/query operations that return many results
  - `if/else` - For conditional logic based on tool results
  - `try/catch` - For error-prone operations (invoke, execute, provision)
  - `poll` - For status checking and waiting operations

- **Code Snippet Templates**: 25+ static examples for common tools
  - Playwright browser automation
  - File system operations
  - GitHub API calls
  - Database queries
  - Optional LLM-generated examples via BAML for dynamic tools

- **CLI Commands**: New `pmcp guidance` command
  - `pmcp guidance` - Show current configuration and status
  - `pmcp guidance --show-budget` - Display token cost estimates

- **Comprehensive Tests**: 48 new test cases for guidance system
  - Configuration loading and validation
  - Token budget estimation
  - Pattern hint matching
  - Code snippet template loading
  - 86% test coverage for guidance modules

### Changed

- **MCP Server Instructions**: Updated to include code execution philosophy
- **Summary Templates**: Enhanced with progressive disclosure messaging
- **BAML Prompts**: Updated to emphasize code execution patterns

### Technical Details

- Token budget optimized: ~200 tokens in minimal mode (80% reduction vs naive approach)
- Hybrid static/LLM approach: Static templates for manifest tools, LLM generation for dynamic tools
- Graceful degradation: System works without BAML or missing template files
- No breaking changes: All existing functionality preserved

## [1.0.0] - 2025-12-29

### Added

- **MCP Gateway Server**: Meta-server that aggregates multiple MCP servers behind a single connection
- **Progressive Tool Discovery**: 9 gateway tools instead of exposing all downstream tools directly
  - `gateway.catalog_search` - Search available tools with filters
  - `gateway.describe` - Get detailed tool schemas
  - `gateway.invoke` - Call tools on downstream servers
  - `gateway.health` - Check server status
  - `gateway.refresh` - Reload server configurations
  - `gateway.request_capability` - Natural language capability matching
  - `gateway.sync_environment` - Detect available CLIs
  - `gateway.provision` - Install MCP servers on demand
  - `gateway.provision_status` - Track installation progress

- **BAML-Powered Capability Matching**: Intelligent matching of user requests to available CLIs or MCP servers
- **CLI Preference**: Prefers installed CLIs (git, docker, etc.) over MCP servers when appropriate
- **Dynamic Server Provisioning**: Install and connect to MCP servers at runtime via npx/uvx
- **Process Handoff**: Seamless adoption of npx-started servers into the gateway
- **Auto-Start Servers**: Playwright and Context7 servers start automatically
- **Server Manifest**: Curated list of 25+ MCP servers with install instructions
- **Policy Management**: Server/tool allowlists, denylists, and output processing

### Technical Details

- Pure Python implementation using `asyncio`
- JSON-RPC over stdio for MCP communication
- Supports both npm (npx) and Python (uvx) MCP servers
- Environment variable support for API keys via `.env` files
