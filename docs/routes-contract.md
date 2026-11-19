# Public route and origin contract

## Parent integration API

`OriginBindings(ttl=3600, max_entries=1024)` lives in `session_origin.py`. Create one
instance per loaded plugin profile. Register an async `/gws-bind` command with a
wrapper (or `functools.partial`) fixing `profile` to the plugin instance's profile:

```python
async def bind(raw_args, *, source=None, session_id="", session_key="", platform=""):
    return await bindings.bind(raw_args, source=source, session_id=session_id,
                               session_key=session_key, platform=platform, profile=profile)
ctx.register_command("gws-bind", bind, description="Bind GWS watches to this conversation")
```

Only the public command dispatcher may supply this context. Do not register bind
as a model tool. The handler accepts no destination arguments. Actor identity is
`source.user_id`, never USER or a session-environment fallback. The binding stores
only whitelisted source fields: profile, principal/user_id, platform, chat_id,
thread_id, session_key, physical session_id, chat_type, scope_id, parent_chat_id.
No message body, credentials, arbitrary source metadata, or global environment is
stored. Bindings are an in-memory, locked, bounded TTL map; expired entries are
cleaned on bind/read. Capacity exhaustion refuses rather than evicting active
bindings. Conflicting reuse of a physical ID refuses.

A tool wrapper must call `bindings.origin(kwargs.get("session_id"), profile)` and
supply that result to `runtime.origin`. Never take session_id or destination from
model arguments. No ID, unknown ID, expired binding, or wrong profile fails closed.
Run `/gws-bind` after `/new`, plugin restart/reload, or expiry. A command issued
before the host has a physical session cannot bind: first establish a conversation,
then bind. Delegated children do not inherit bindings, even when their environment
inherits the parent's routing fields and changes only its physical ID.

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
Physical IDs are deliberately not in operator configuration: `/new` requires a new
command binding, but need not recreate the operator route. Non-default profiles
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

- `hermes_cli/plugin_command_dispatch.py:11-32` forwards only declared trusted
  command context. The tests execute this actual function, not a replacement.
- `gateway/run_inbound_hooks.py:279-319` provides source, physical session_id,
  session_key and platform to commands, including before the agent-turn binding.
- The public API inventory `hermes_cli/data/plugin_api.json` exposes
  `gateway.session_context:get_session_env`, but no exact physical-ID-bound trusted
  origin getter. That getter can fall back to process environment; route fields
  can also be inherited by a delegated child whose ID is rebound. Public
  `hermes_cli.plugin_session` has no authenticated actor+route binding API either.
  Therefore explicit command binding is necessary, not automatic hook correlation.
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

1. Tool kwargs identify a physical session, not the current human actor. Binding
   captures the actor who ran the command, not proof of each later caller. Use
   sender-isolated sessions/trusted conversations; this API cannot provide per-turn
   actor authorization in shared multi-user physical sessions. Conflicting rebinds
   refuse, but do not solve that host limitation.
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
5. This task tests synthetic sources, real public command invocation, and loopback
   HTTP redirect refusal. It does NOT demonstrate live Hermes receiver delivery,
   real account auth, or the CI minimum-host version; those remain release gates.
