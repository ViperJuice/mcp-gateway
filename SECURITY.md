# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.0.x   | ✅ Active  |
| 1.22.x  | ✅ Security fixes only |
| < 1.22  | ❌ No longer supported |

2.0.0 is a breaking release: `GET /mcp` is retired (405) and the
`PMCP_KEEPALIVE_MAX_SECONDS` lifetime cap is removed with no replacement by
design. See the CHANGELOG before upgrading. 1.22.x receives security fixes
only; it is the last 1.x line and is pinned to `mcp` 1.x, which no longer
receives upstream releases.

## Threat Model

PMCP is a local-first MCP gateway. Its default security posture assumes:

- **Bind address**: `127.0.0.1` (loopback only). The HTTP port is not exposed to external
  networks unless you explicitly bind to `0.0.0.0` or place it behind a reverse proxy.
- **Trust boundary**: processes running on the same host as PMCP are trusted. Remote clients
  (via reverse proxy) are untrusted and must present a valid Bearer token when
  `shared-secret` or `resource-server` auth is configured.
- **TLS**: PMCP does not terminate TLS. For any network exposure, terminate TLS at a reverse
  proxy (nginx, Caddy) and proxy to `127.0.0.1:3344`. See the README for example configs.

### What PMCP protects against (when correctly configured)

- Unauthenticated tool invocations via Bearer token guard on `/mcp`
- AS-issued access tokens in `resource-server` mode via JWKS signature,
  issuer, expiry, not-before, and audience validation. The audience is bound to
  the operator-configured canonical resource URI (`resource_server_audience`,
  per RFC 8707) and is never derived from the request Host header. Signatures
  are only accepted for the operator-configured algorithm allowlist (default
  `RS256`/`ES256`); the token's own `alg` header is never trusted. The mode
  fails closed at startup without an issuer, JWKS URL, and audience, and the
  JWKS URL must be `https` and is rejected when its host is a non-public IP
  literal (see the DNS-name limitation below). JWKS is fetched asynchronously and
  cached so validation never blocks the event loop; an unreachable JWKS endpoint
  returns `503` while an invalid or wrong-audience token returns `401`.
- Timing oracle attacks on token comparison (`hmac.compare_digest`)
- Request floods via per-source-IP sliding-window rate limiting (`--rate-limit`
  / `PMCP_RATE_LIMIT`) on `/mcp`
- Oversized payloads causing OOM (`Content-Length > 10 MB → 413`)
- Hanging downstream tools consuming connections indefinitely (60 s request
  timeout, `--request-timeout` / `PMCP_REQUEST_TIMEOUT`) — applies to every
  `/mcp` POST **except** `subscriptions/listen`, which is a long-lived stream
  by design; see "Known limitations" below for what bounds it instead.
- Unbounded long-lived streams on `/mcp` (`PMCP_MAX_LISTEN_STREAMS`, default
  64, bounds concurrent `subscriptions/listen` subscriptions; retired GET's
  pre-session keep-alive concurrency cap is this guard's one-for-one
  predecessor)
- Multiple gateway instances fighting over resources (fcntl singleton lock)
- Reconnect storms from crashing downstream servers (per-server reconnect flag)
- **Unilateral credential relaxation**: a manifest server's `requires_api_key`
  can only be relaxed by a variable the entry itself names in
  `api_key_optional_when` — an operator's overlay can supply that variable's
  value, but cannot make a server's credential optional unless the manifest
  entry already declared it relaxable. A server also cannot name its own
  credential as its relaxer. Every unset, malformed, self-referencing, or
  placeholder (`${VAR}`) relaxer value fails closed and the credential stays
  required (Consiliency/pmcp#114).
- **Mutable CI dependencies**: every GitHub Action this repository runs — in
  `.github/workflows/` and in the local composite action under
  `.github/actions/` — is pinned to a full commit SHA with the release named in
  a trailing comment (`owner/action@<sha> # vX.Y.Z`). A tag or branch is
  mutable: whoever controls the action's repository controls what runs with
  the job's permissions, and the release workflow holds `id-token: write` for
  PyPI trusted publishing. `scripts/check_workflows.py` fails CI on any remote
  `uses:` that is not in that form, and on any change to the release
  workflow's exact action set; Dependabot maintains the pins, including a
  dedicated entry for the composite action's directory
  ([#217](https://github.com/Consiliency/pmcp/issues/217)).

### Known limitations

- **The auth-URL host check filters IP literals only**: a public auth metadata
  or JWKS URL is rejected when its host is a non-public IP literal — private,
  CGNAT, link-local, loopback, multicast, site-local or unspecified, including
  IPv4 addresses embedded in IPv6 literals (RFC 4291 mapped and compatible,
  RFC 6052 NAT64, RFC 3056 6to4, RFC 4380 Teredo, RFC 5214 ISATAP) and legacy
  numeric forms such as `2852039166` or `0177.0.0.1`. **A DNS name is accepted
  without being resolved**, so a name pointing at an internal address is not
  caught. PMCP does not resolve names deliberately: a lookup is
  TOCTOU-vulnerable and is not SSRF defence without connection-time IP pinning.
  What it does instead is stop claiming otherwise — **a server-supplied URL is
  relayed unverified and presented as such**, via
  `UrlElicitationInfo.url_verified`, `AuthMetadataInfo.verified_urls` (one entry
  per URL field), and `AuthChallengeInfo.resource_metadata_url_verified`, all
  defaulting to unverified, with the caveat carried in the `next_step` string an
  agent follows and in `pmcp auth` output. Paths where PMCP fetches the URL
  itself fail closed and require a verified public literal, and a URL from a
  downstream server's payload may not be loopback `http://`
  ([#211](https://github.com/Consiliency/pmcp/issues/211)).
- **No mTLS**: clients are not authenticated by certificate; only Bearer token.
- **No per-tool ACL on HTTP**: any valid token can invoke any tool. Tool-level policy is
  enforced at the MCP layer, not the HTTP layer.
- **Rate-limit source IPs may be shared**: localhost clients usually share the
  same observed source IP, and reverse-proxied clients may share one bucket
  unless the proxy preserves distinct client IPs for PMCP.
- **`/health` and `/metrics` are unauthenticated by design**: load balancers and Prometheus
  scrapers typically cannot present Bearer tokens. Bearer auth for `/mcp` does
  not protect these endpoints. Do not expose them on a public interface without
  separate network-layer control (firewall rule, IP allowlist, or reverse-proxy
  policy).
- **Resource Server, not Authorization Server**: PMCP can validate
  Authorization Server issued access tokens in `resource-server` mode, but it
  does not provide an Authorization Server, DCR, SSO, RBAC, billing, or a
  complete multi-tenant identity service.
- **Authorization discovery is diagnostic**: PMCP can surface protected-resource,
  authorization-server, OIDC discovery, Client ID Metadata Document, scope, and
  URL-mode elicitation hints, but it does not store third-party OAuth refresh
  tokens.
- **URL-mode elicitation is out of band**: never paste OAuth codes, third-party
  passwords, or provider refresh tokens into gateway tools. `gateway.auth_connect`
  accepts API-key credentials only for local env-store flows; URL-mode flows only
  accept an elicitation identifier and consent acknowledgement.
- **Redaction is best-effort defense in depth**: PMCP redacts bearer tokens, API
  keys, bare provider tokens (`sk-`, `ghp_`, `github_pat_`), common secrets, URL
  userinfo, authorization codes, and auth-bearing query parameters from gateway
  outputs, status/doctor diagnostics, feedback payloads, and HTTP diagnostics.
  Redaction is applied across every task-emitting surface — `gateway.invoke`,
  `gateway.tasks_result`, `gateway.tasks_list`, and `gateway.tasks_get`, including
  task `status_message` and raw fields — and truncation summaries are built from
  post-redaction text. Treat all logs as operational data and avoid adding
  secrets to server names, tool names, or free-form descriptions.
- **Credential isolation is scoped, not identity-complete**: user-scope env-store files are owned by
  the local OS account, project-scope env-store files are owned by the project
  directory, and remote header placeholders resolve from those stores plus
  process environment in non-tenant mode. Tenant remote-header mode reads
  tenant-scoped files derived from the resolved project root and must not read
  another tenant's file, but PMCP still does not provide cross-user identity or
  authorization isolation by itself.
- **The project `.env` does not reach downstream servers**: PMCP reads a plain
  `.env` for its own configuration at startup, and its credential-availability
  check loads env files to answer a boolean. Both record which keys those loads
  introduced into the gateway's environment, and `sanitized_subprocess_env`
  strips exactly those keys from every spawned server, so an unrelated secret in
  the operator's `.env` is no longer inherited by third-party MCP servers. A
  server's own declared `env_var` is still supplied from a plain `.env` — that
  key only, into that server only. Stripping is by recorded provenance, not by
  key name: a variable the operator exported into their shell that merely also
  appears in `.env` is untouched. Secrets the operator exported into the shell
  itself are still inherited — deliberately, and out of scope
  ([#229](https://github.com/Consiliency/pmcp/issues/229)).
- **Tenant code-mode hosting keeps execution outside PMCP**: the host contract
  in `specs/tenant-code-mode-host-contract.md` treats PMCP as the broker and
  the companion tenant server as the sandbox execution authority. The contract
  does not add PMCP-owned sandbox isolation, tenant auth, or durable execution
  logs. PMCP policy can allow or deny `tenant-code-mode` and
  `tenant-code-mode::*`, apply output caps, and redact returned diagnostics, but
  production multi-tenant isolation still requires companion-server and
  deployment controls.
- **Subprocess spawning**: PMCP forks child processes for downstream MCP servers. A malicious
  MCP server config entry could cause PMCP to spawn arbitrary executables. Only configure
  servers you trust.
- **No audit log persistence**: the per-call audit log (`tool_call tool=... ok=...`) is
  written to stderr/stdout, and structured `gateway.health.audit_events` are
  bounded in memory. There is no database, log rotation, or tamper-evident
  storage.
- **Trace context is metadata, not identity**: PMCP preserves accepted
  `traceparent`, `tracestate`, and `baggage` strings only through explicit
  PMCP-owned fields or request metadata. Do not put bearer tokens, API keys,
  auth codes, user identifiers, or other secrets in trace baggage.
- **MCP task records are transient**: task IDs are downstream server identifiers
  held in gateway memory for visibility and cancellation. They are not durable
  audit records and do not provide cross-user authorization isolation on
  unauthenticated local transports.
- **Two protocol eras are served on one endpoint, through the same policy
  gate**: the `MCP-Protocol-Version: 2026-07-28` header plus a `params._meta`
  envelope route a request to the modern era instead of the
  `initialize`-negotiated handshake era (`2024-11-05`–`2025-11-25`) — see
  README's protocol-negotiation section. Both eras dispatch through the same
  registered handlers, so the same policy, audit, and input-schema validation
  applies regardless of which era selected the request; the modern era is not
  a lower-trust bypass. Unsupported draft extensions beyond the six proxied
  operations, `server/discover`, and `subscriptions/listen` remain out of
  scope until PMCP explicitly claims them.
- **`subscriptions/listen` has no absolute-lifetime cap, deliberately**: it is
  exempt from the `request_timeout` wrapper that bounds every other `/mcp`
  POST, because that wrapper silently truncated every subscription stream at
  `request_timeout` seconds (60s by default) with no graceful close frame —
  the defect this exemption fixes, not a property worth preserving for a
  connection that is long-lived by design. What bounds exposure instead:
  `PMCP_MAX_LISTEN_STREAMS` caps concurrent subscriptions (default 64, same
  as the retired pre-session keep-alive's concurrency cap), the SDK's own
  per-stream event-backlog cap bounds a single subscription's memory, and
  `/mcp` auth applies to `subscriptions/listen` exactly as to every other
  method whenever `auth_mode` is configured. There is no configurable
  replacement for the retired `PMCP_KEEPALIVE_MAX_SECONDS` absolute-lifetime
  bound; an operator who needs one must enforce it at a reverse proxy or load
  balancer in front of PMCP.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report via **GitHub private security advisory**:
[https://github.com/ViperJuice/pmcp/security/advisories/new](https://github.com/ViperJuice/pmcp/security/advisories/new)

Include:
- A description of the vulnerability and its impact
- Steps to reproduce (proof-of-concept if possible)
- Affected versions
- Any suggested mitigation

**Response timeline**:
- Acknowledgment within **7 days**
- Fix or mitigation plan within **30 days** for critical/high severity
- Coordinated disclosure after patch is available

## Security Hardening Checklist

Before exposing PMCP beyond localhost:

- [ ] Set `PMCP_AUTH_TOKEN` (do not use `--auth-token`; token visible in `ps aux`)
- [ ] Terminate TLS at your reverse proxy; proxy to `127.0.0.1:3344`
- [ ] Bind to loopback (`--host 127.0.0.1`, the default)
- [ ] Set `--rate-limit` appropriate for your traffic (e.g. `60` for 1 req/sec per observed source IP)
- [ ] Firewall `/health` and `/metrics` to internal networks only
- [ ] Run as a non-root user (Docker image already uses `appuser`)
- [ ] Review downstream MCP server configs — only trust servers you control
- [ ] For `tenant-code-mode`, keep `${TENANT_CODE_MODE_MCP_TOKEN}` and
      `${TENANT_CODE_MODE_TENANT_ID}` in env stores or process environment, not
      config files or logs
- [ ] For hosted tenant runs, use `gateway.tasks_cancel` with downstream task
      IDs and keep durable logs, artifacts, artifact retention, tenant auth,
      SSO/RBAC, and billing outside PMCP
