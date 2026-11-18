# Manual OAuth client setup required

**Not catalog-admitted:** native `gws` credential handling conflicts with current Hermes OAuth policy. Read [the full disclaimer](docs/catalog-policy.md) and [Google Cloud setup instructions](docs/oauth-setup.md) before connecting accounts.

Plugin activation creates the private directory:

```text
$HERMES_HOME/gws-oauth/
```

Check the installer result for live gateway reload; support depends on the host. Begin a fresh session for updated tool schemas. For CLI, TUI, Desktop or API use, load the updated plugin in the frontend's agent process and open a fresh session; no messaging gateway is required.

Before calling `gws_login`, configure a Google Desktop OAuth client in either location:

```text
~/.config/gws/client_secret.json                 # native global gws client
$HERMES_HOME/gws-oauth/client_secret.json        # optional profile override
```

The profile file wins when both exist. Set file permissions to `0600`. Do not paste or upload the JSON through chat, and do not store it inside the plugin directory. Authorized accounts remain profile-scoped even when the client is global.

To renew an account that is already configured, call `gws_login` with `reauthorize=true` after this version is installed. The previous credentials stay in place until the new browser flow reports the requested identity. A mismatch, cancellation, or timeout leaves them intact; a failed replacement attempts to restore them. Avoid simultaneous native commands during the brief swap. See README for crash-recovery limitations. Use `gws_accounts(check_health=true)` for an optional live check that distinguishes stored credentials from a session confirmed by native `gws`.

If the browser runs on the same host as Hermes, the redirect reaches the `gws` loopback directly and the login completes without pasting anything; the browser shows "Success". Confirm with `gws_accounts` or by calling `gws_login` with the same `account` and `action="status"`. Existing accounts and other profiles are untouched by this upgrade; a running gateway must be reloaded for the new behaviour to take effect.

Run `gws auth status` to inspect the exact native `client_config` path on systems using a legacy location.

## Account and command policy

One profile belongs to one trusted operator; connect that operator’s Google accounts only. Every authorized session in the profile can use those accounts. Full OAuth scope mode remains the default; the configured client and Google consent determine what can actually be granted.

Prefer curated Gmail/Drive/Calendar tools for common operations, `gws_schema` for native request discovery, and `gws_api` for the rest. Public tools refuse native auth export/login/logout/setup commands; use `gws_login`, `gws_logout` and `gws_accounts(check_health=true)` instead. Native `gws auth` remains available in an operator terminal when configured; its client JSON is reusable, but global account credentials are not automatically imported. Local logout does not revoke Google consent; stop watches and finish cleanup first. Mutations, destructive operations and local file input/output use Hermes approval, not a model-supplied confirmation flag.

## Targeted watches are opt-in

The Gmail/Drive/Calendar watch tools are registered after upgrade, but nothing is subscribed or started by installation. `new_session` starts need a native Hermes webhook route, and Drive/Calendar need `watch_callback_url`; Gmail needs a Pub/Sub topic and subscription. No worker starts automatically, and no Google resource is created until an explicit `gws_*_watch action=start` and the separately supervised worker run. See `docs/watch-setup.md`.

Same-session watches use the existing GOWA-style `pre_gateway_dispatch` bridge: check the stored physical session against the current session, then call `ctx.inject_message(session_key=...)`. No Hermes core patch is required. A reset observed before injection stops forwarding; a concurrent `/new` between this check and the host lookup can still race. Injection acceptance means scheduled, not delivered; ambiguous injections are not replayed.
