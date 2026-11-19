# Public route and origin contract

## Parent integration API

`current_origin(session_id, profile)` snapshots the current tool call's bound
runtime context. The wrapper supplies the host-dispatched physical `session_id`
and the loaded plugin profile, never model arguments. It requires matching bound
ID/profile, a non-empty current actor, route key, platform and chat. Optional thread,
scope and parent-chat metadata is copied without message content or credentials.

No manual bind command, TTL map or process-global environment fallback exists.
Each call gets a fresh snapshot, including after `/new` or plugin reload. Delegated
children are refused before resolving origin, even if a child inherits its parent's
route and rebinds its own physical ID. Webhook, scheduled and non-conversational
surfaces are refused. Mixed/cleared actor turns never fall back to route ownership.

This deliberately uses internal `gateway.session_context._bound_session_value`
and `agent.delegation_context.is_delegated_child_process_context`, because the
public environment getter can borrow process-global routes and inherited child
context. It is a narrow compatibility exception, not a public-API-only claim.
Missing APIs/context fail closed. No Hermes core patch is required.

`RouteManager(ctx=None, *, home=None, store=None, config=None, profile="default")`
uses the existing WatchStore transaction API. Pass the SAME profile-local store to
host and worker. `config` is the operator route object below, or a zero-argument
callable returning its current value. An omitted config fails closed at create;
there is no automatic native-store discovery. Prefer a callable reloading settings
through the parent's supported config loader for long-running workers. A static
mapping detects in-memory changes/reloads, not edits to a file it never rereads.

`create(watch, spec, origin)` returns plugin-local per-watch `name`, not the shared
operator route name. Keep this name in existing watch route_name/bridge_name fields.
`read(name)` returns only local non-secret metadata. `remove(name, watch_id)` removes
only matching plugin metadata and reports operator_route_preserved. No host route
is ever created, changed, disabled, or deleted. Metadata lives in `route_bindings`
in the existing watch SQLite database, preserving the existing restrictive directory.

Register `bridge_hook(store, ctx, routes, profile=profile)` on public
`pre_gateway_dispatch`. The callback accepts `event`, `session_store`, and other
host hook kwargs. Parent's existing bridge_callback wrapper must forward routes
and profile. Worker must construct RouteManager with the same config and profile;
`RouteManager(home=...)` alone deliberately cannot send.

## Small operator configuration

Place this object under a parent-selected plugin setting (recommended
`plugins.entries.google-workspace.settings.watch_routes`). Do not put it in an
installed snapshot or expose it through a model tool. Supply strong random secrets
of at least 32 characters using the host's protected configuration/secrets setup.
Values below are placeholders, not credentials.

```yaml
routes:
  - origin:
      profile: default
      principal: USER_ID
      platform: discord
      chat_id: CHAT_ID
      thread_id: null
      session_key: EXACT_HOST_ROUTE_KEY
    new_session:
      name: gws-notify
      url: https://gateway.example/webhooks/gws-notify
      secret: REPLACE_WITH_A_STRONG_RANDOM_HMAC_SECRET
      destination: {platform: discord, chat_id: CHAT_ID, thread_id: null}
      skills: []
      enabled: true
    same_session:
      bridge_name: gws-watch-bridge-main
      url: https://gateway.example/webhooks/gws-watch-bridge-main
      secret: REPLACE_WITH_A_DIFFERENT_RANDOM_HMAC_SECRET
      enabled: true
```

Each origin is an exact six-field match, including null thread_id; no wildcard or
model-selected route. At most 1024 configured entries. Duplicate matches refuse.
Physical IDs are deliberately not in operator configuration: a new watch after
`/new` captures the new physical context without recreating the operator route. Non-default profiles
must use `/p/PROFILE/webhooks/NAME`. HTTPS is required except loopback HTTP. No
URL credentials, query strings, fragments, or redirects are accepted. The bridge
name MUST start with `gws-watch-bridge-`; the hook suppresses this namespace even
when config is missing/invalid or raw_message is malformed.

The operator provisions routes with the public `hermes webhook subscribe` CLI
(consult that host's `--help` for its exact flags). The plugin never imports CLI
implementation helpers or reads webhook_subscriptions.json. Operator requirements:

- Native receiver: generic HMAC verification, exact profile, enabled route. Use
  `events: [gws-watch]` for new-session and `[gws-watch-bridge]` for bridge.
- New-session: `deliver` and fixed `deliver_extra.chat_id/thread_id` must equal the
  configured destination; no payload templating in destination. `deliver_only`
  and `mirror_to_session` must both be false. Configure the chosen skills directly
  on the operator route; requested watch skills must exactly match plugin config.
- New-session prompt: include `{__raw__}` and describe its events as untrusted
  external metadata, never instructions to mutate Google data or follow links.
  The plugin adds the locally stored watch instructions as `instructions` in the
  signed payload. The operator prompt should distinguish those owner instructions
  from untrusted events, and report only requested changes.
- Bridge: `deliver: log`, no skills, no mirroring, no deliver-only bypass and no
  coalescing/scheduled execution that bypasses the hook. Use a fallback prompt
  saying this is an internal bridge, perform no actions, and reply NO_REPLY.
  Prompt text is defense in depth, not a substitute for the hook.
- Enable the documented `plugins.entries.google-workspace.allow_gateway_injection:
  true` consent setting for same-session. No private consent-probe method is used;
  callable API availability is not proof of consent, and the injection return is
  authoritative for admission.

The plugin hashes the exact selected config including its secret, stores only the
hash and non-secret per-watch metadata, and rechecks config before send and bridge
injection. Edits/disable/secret rotation require stop and recreate; they never
silently retarget an old watch. A removed operator config cannot be reconstructed
from plugin state. Provision a route separately for each exact native destination.

## Host evidence and limits

Inspected real host `/home/xithus/.hermes/hermes-agent`:

- `gateway/session_context.py` supplies bound-only ContextVar reads and clears
  actor identity when the active turn event becomes mixed/anonymous. The adapter
  requires those bound values, not public `get_session_env`'s environment fallback.
- `agent/delegation_context.py` supplies a context-local/process delegation guard.
  Tests exercise the real `delegated_child_context`, including physical-ID rebinding.
  An ID equality check alone does not prove that a parent route belongs to a child.
- These internal contracts must be revalidated on host upgrades. Tests use real host
  helpers with synthetic route values; they do not substitute for live delivery.
- `gateway/platforms/webhook.py:791-825` builds native authenticated webhook
  source platform, `chat_name=webhook/NAME`, `user_id=webhook:NAME`, routed profile,
  and raw payload. The hook requires that exact source identity/profile plus the
  configured bridge and matching durable watch, origin, generation and outbox row.
  It trusts Hermes native ingress authentication; the hook is not another HTTP
  authentication layer. Forged source objects from another trusted plugin are
  outside the isolation boundary, as are malicious operators.
- `hermes_cli/plugins_context.py:397-404` and public plugin docs expose injection
  with session_key/origin, but NO expected_session_id. The bridge prechecks the
  physical session through hook-provided `session_store.lookup_by_session_key`,
  then transactionally claims outbox before injection. `/new` between lookup and
  host admission can still target a replacement session. This accepted residual
  reset race is explicitly tested and is NOT race-free delivery.

Important limitations:

1. Tool kwargs alone identify a physical session, not an actor. The adapter also
   requires the current bound actor and refuses mixed/cleared actor turns. This
   depends on host context propagation and is not isolation against a malicious
   plugin or operator. Use trusted conversations or sender-isolated sessions.
2. Operator config is an attestation of the remote native route's destination,
   prompt, profile, skills and signature mode. There is no public readback API here
   to prove the receiver still matches it. Direct receiver edits without updating
   plugin config cannot be detected. Stop watches before editing receiver routes;
   verify configuration with public CLI. Do not claim receiver-side pinning.
3. Native HTTP accepted/duplicate receipts mean admission, not completion or actual
   external delivery. Stable request IDs and HMAC v2 timestamp/body signatures
   preserve the durable worker retry contract; replies are read at most 65536 bytes.
   No redirects are followed, including same-origin redirects.
4. Same-session true means scheduled/admitted. False or exception after claiming
   becomes uncertain and is not blindly replayed. Crash recovery retains the
   existing durable outbox uncertainty behavior. No exactly-once guarantee.
5. Origin regression tests exercise real host ContextVars with synthetic routes,
   delegated children, concurrent contexts and actor clearing. Route tests include
   loopback HTTP redirect refusal. These do not prove live automatic-origin watch
   delivery or fresh OAuth; see [verification](verification.md) for separately
   measured end-to-end coverage.
