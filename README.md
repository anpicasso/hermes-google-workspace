# Hermes GWS OAuth

Multi-account OAuth login, native `gws` passthrough, and targeted Gmail, Drive and Calendar watches for the [Google Workspace CLI (`gws`)](https://github.com/googleworkspace/cli) in Hermes Agent.

The plugin keeps each account inside the active Hermes profile, combines curated Workspace tools with a guarded native `gws` escape hatch, and lets a user finish the otherwise-local OAuth callback from any Hermes platform. It does not share tokens across profiles or modify Hermes core. Targeted watches run deterministic filters before any model invocation; see [docs/watch-setup.md](docs/watch-setup.md).

## Prerequisites

- **Node.js 18+** — required only when installing `gws` through npm. You can instead download a pre-built binary from [Google Workspace CLI releases](https://github.com/googleworkspace/cli/releases) or use another supported package manager.
- **A Google Cloud project** — required for OAuth credentials. Create one through the [Google Cloud Console](https://console.cloud.google.com/), the `gcloud` CLI, or `gws auth setup`.
- **A Google account with access to Google Workspace.**
- **Hermes Agent** with a stable session identity: CLI, TUI, Desktop, API server, a messaging gateway, or another frontend.

Install `gws` using one of its supported methods. For npm:

```bash
npm install -g @googleworkspace/cli

gws --version
```

## Install

Install and enable the plugin in the desired Hermes profile:

```bash
hermes plugins install anpicasso/hermes-gws-oauth --enable
```

For a named profile:

```bash
hermes --profile <name> plugins install anpicasso/hermes-gws-oauth --enable
```

Authorized accounts always remain profile-scoped. The OAuth client may be shared from the native `gws` configuration or overridden by one Hermes profile. Repeat the installation for every profile that should expose the tools.

### Configure the OAuth client manually

For security, the plugin **never accepts, downloads, or installs `client_secret.json` through chat**. Plugin activation creates `$HERMES_HOME/gws-oauth/` with mode `0700`. Installation with `--enable` reloads a running gateway immediately. A non-gateway frontend loads the plugin when its agent process starts; no messaging gateway is required.

`gws_login` selects the OAuth client in this order:

1. `$HERMES_HOME/gws-oauth/client_secret.json` — optional profile override.
2. The native global `gws` client, normally `~/.config/gws/client_secret.json`.

If `gws auth login` already works from its native client file, no second copy is required. Setups that rely on `GOOGLE_WORKSPACE_CLI_CLIENT_ID`, `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET`, or `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` still need one of the files above because the plugin deliberately clears inherited Google credential overrides. The login flow creates neither file.

`gws auth status` reports the exact native path as `client_config` if your platform uses a legacy location.

1. Configure the required Workspace APIs and OAuth consent screen in Google Cloud.
2. Create an OAuth client of type **Desktop app**. If the consent screen is in *Testing*, add every account under *Test users*.
3. Download the client JSON and install it globally for native `gws`:

```bash
install -d -m 700 "$HOME/.config/gws"
install -m 600 /secure/path/client_secret.json \
  "$HOME/.config/gws/client_secret.json"
```

   Or override it for one Hermes profile:

```bash
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
install -m 600 /secure/path/client_secret.json \
  "$HERMES_HOME/gws-oauth/client_secret.json"
```

The profile override location is:

```text
$HERMES_HOME/gws-oauth/client_secret.json
```

Do not paste this file into a chat, commit it, or place it inside the plugin directory. The selected client must be a regular file, not a symbolic link. Only the client may be shared; authorized accounts and their tokens remain under the active Hermes profile.

If the gateway cannot find `gws`, set an absolute binary path:

```bash
hermes config set plugins.entries.gws-oauth.settings.gws_bin /absolute/path/to/gws
```

For terminal chats, close and reopen Hermes to load plugin code changes. For gateway chats, restart the gateway from an external shell and begin a new session.

## Use

Ask Hermes from any platform: CLI, TUI, Desktop, API server, or a gateway DM, group, channel, or thread:

> Connect `person@example.com` to Google Workspace with read-only access.

Hermes calls `gws_login` with one of the native `gws` permission modes:

- `default`
- `readonly`
- `full` (default)
- `custom` with explicit OAuth scopes

Open the returned Google URL and grant access. Two outcomes are possible, and both are handled:

- **Browser on another device** (phone, laptop): the redirect to `localhost` fails in the browser. Copy the **complete URL from the address bar** and paste it into the same chat. Both `http://localhost:...` and Safari's protocol-less `localhost:...` form are accepted. In gateway chats the plugin forwards the code to the waiting local `gws` process and rewrites the message before it reaches the agent.
- **Browser on the Hermes host**: the redirect reaches the `gws` loopback server directly, the browser shows "Success — you may now close this window", and `gws` exits by itself. A watcher thread validates the identity and commits the credentials without any paste. The attempt stays registered until the deadline so that a late paste of the same URL is still intercepted and answered with the stored outcome. To read that outcome, call `gws_login` again with the same `account` and no `callback_url`: the stored result is returned exactly once (`consumed: true`) and the attempt is released; `gws_accounts` also shows the account immediately.

Outside gateway ingress there is deliberately no interception: the callback reaches the model and remains in chat history. Hermes completes the pending login by calling the same `gws_login` tool again with the original `account` and the pasted `callback_url`. Explicit completion is available on every platform, including gateways when the callback reaches the agent. No terminal command or core patch is required.

One Hermes profile belongs to one trusted operator; multiple accounts are that operator’s Google accounts, not separate tenants. Every authorized session in that profile can use every connected account. Restrict gateway access accordingly; use separate profiles for different operators. A login expires after five minutes, with one pending attempt per user/chat. The Google identity returned by `gws` must match the requested account.

Every failure carries a stable, secret-free `reason`. Exchange failures: `invalid_callback`, `expired`, `callback_rejected`, `gws_failed`, `identity_unconfirmed`, `identity_mismatch`, `account_exists`, `commit_failed`, `internal`; they appear in the tool result and in the rewritten chat message, e.g. `no se pudo completar el login (identity_mismatch: ...)`. Pre-flight failures: `invalid_arguments`, `no_session_identity`, `client_missing`, `launch_failed`, `pending`, `account_exists`, `no_pending`. Codes, URLs and native output are never echoed. `identity_mismatch` adds one safe detail — a masked form of the identity Google actually reported (`a***o@kaito.io`) next to the requested account — because the usual cause is a misspelled request; the full reported address is never echoed. `identity_unconfirmed` is reported when `gws` prints `(unknown)` (its userinfo call failed), which is not a mismatch.

To renew an account that is already configured, call `gws_login` with `reauthorize=true`. Without that explicit flag the plugin refuses to touch an existing account. The previous credentials stay in place while the new browser flow runs; they are replaced only after `gws` reports the requested identity and the new `credentials.enc` exists. A mismatch, cancellation, or timeout preserves the old credentials; a failed replacement attempts to restore them. Preserving credentials does not make an expired authorization valid. Because one chat has one pending attempt, start renewal links sequentially or from separate chats.

## Tools

Common tasks use the curated Gmail search/read/draft/send, Drive search/read/upload/share, and Calendar list/freebusy/create/update tools. `gws_schema` exposes native method discovery; `gws_api` covers the rest through the same approval policy. See [tool mappings, examples and approvals](docs/api-tools.md) and the [tested compatibility baseline](docs/compatibility.md).

- `gws_login(account, scope_mode="full", scopes=[], callback_url=None, reauthorize=False)` — starts OAuth for one account on any platform. Omit `callback_url` to start; pass it to finish an existing attempt with the same profile, surface, session and user identity. Set `reauthorize=true` only to renew an account that already exists; old credentials remain until the new identity is validated and committed.
- `gws_accounts(check_health=False)` — lists the account labels stored in the active profile without revealing credentials. With `check_health=true`, runs native `gws auth status` per account (up to 20 seconds each) and returns `state` (`valid`, `invalid`, `stored`, `unreadable`), `stored` and `valid` separately, `reauth_required`, a sanitized granted-scope summary, and a short `detail`. `stored` means the files exist but the live session was not confirmed; `valid` means native `gws` confirmed it. Health checks leave decryption and refresh to `gws`; the separate Pub/Sub worker exception is documented below.
- `gws_api(account, args)` — executes supported native Workspace commands for one explicit account after command-policy validation (for example `["drive", "files", "list"]`). On failure it returns `error_code`, `category`, `action`, `reauth_required`, and `exit_code` with a sanitized message; raw stdout/stderr are never returned because they can contain request data or token fragments.
- `gws_gmail_watch` / `gws_drive_watch` / `gws_calendar_watch` — `start | status | stop` for bounded targeted watches. Start requires an exact registered account, a service-specific `target`, and `delivery_mode=same_session|new_session`. Selectors are literal and deterministic; matching happens before a model runs. `status` without `watch_id` lists only the calling principal's watches for that service. `stop` requires an exact `watch_id` and reports local revocation separately from pending remote cleanup. See [docs/watch-setup.md](docs/watch-setup.md).

Large successful responses (over 200,000 characters) return `output_file`, `output_bytes`, and `output_encoding="utf-8"` instead of inline `data`. The complete native stdout is saved in a unique private directory under `$HERMES_HOME/gws-oauth/response-*` (directory 0700, file 0600). Read it locally with code; Gmail attachment JSON contains base64url `data` that must be decoded to obtain the PDF. Native `--output` does not save these JSON responses. Delete the response file and its directory after processing; no automatic retention policy is imposed. Output remains buffered in memory. Failed native commands are never spilled. If local saving fails after native success, `output_write_failed` is returned: inspect remote state before retrying a mutating command.

Failure semantics: `invalid_grant`, or `invalid_rapt` only when native output actually contains it, points to `reauthorize=true`; `category=permission` points to scopes or API enablement; `category=syntax` and `category=api` do not suggest reauthorization; `discovery` and `internal` are retryable. A bare exit status 2 is reported as `auth_unavailable` and asks for a health/connectivity check, not automatic reauthorization. Unknown token errors and incomplete health output remain inconclusive (`valid=null`).

Authorized accounts are stored under:

```text
$HERMES_HOME/gws-oauth/accounts/<account>/
```

Each account receives its own `GOOGLE_WORKSPACE_CLI_CONFIG_DIR`.

### Several Hermes profiles on one host

Every profile owns a separate `$HERMES_HOME/gws-oauth/` tree (e.g. `~/.hermes/gws-oauth/` for the default profile and `~/.hermes/profiles/<name>/gws-oauth/` for named profiles). Accounts, staging directories, the `accounts/.lock` file, `.encryption_key`, `credentials.enc` and `token_cache.json` all live inside that tree, so logging an account into one profile never reads or writes another profile's accounts. Each profile runs its own gateway process, so `LoginManager`, the temporary hook and the random loopback ports are independent as well. The only shared artifact is the read-only OAuth client (`~/.config/gws/client_secret.json` or a profile override); sharing one Desktop client across many accounts is normal. With `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` set, native `gws` writes nothing under `~/.config/gws/`. The same Google account may be authorized in two profiles; each holds its own refresh token.

## Targeted watches

The three watch tools share one private SQLite state database and one explicitly supervised worker. Start creates an owned native Hermes webhook route (or a same-session bridge route) before any Google subscription; the worker creates/renews Google push subscriptions, drains deltas, applies selectors locally, and forwards only matching metadata.

```text
Gmail users.watch -> Pub/Sub pull --------------------+
Drive changes.watch -> HTTPS callback ---------------+-> durable hints -> cursor readers
Calendar events.watch -> HTTPS callback -------------+   -> deterministic filters
                                                         -> durable outbox -> native Hermes route
```

Highlights:

- Gmail: exact conversation/message targets or an explicitly broad `mailbox: true`; literal sender/domain/label/subject filters and an optional MIME attachment predicate. History is read with `users.history.list`; Pub/Sub is pulled and acknowledged only after hints/cursors are durable. Message bodies are never fetched for notifications.
- Drive: exact files, direct folder children (not recursive), or explicit `all_files`; literal filename/MIME and `appeared|updated|renamed|moved|trashed|restored|removed` categories. A removal means deleted or no longer accessible; it does not claim a narrower cause.
- Calendar: exact calendars with optional event/series targets; organizer/attendee/title filters, changed-field narrowing, bounded date windows, and selected-attendee RSVP tracking. Recurrence is never expanded unboundedly; series + date window is rejected in this version.
- `minutes` is the local authorization lifetime (1–1440), separate from Google's renewable subscription lease. `batch_seconds` is 0–60 and never delays a one-shot reservation.
- A one-shot match reserves exactly one logical event, stops collection, and retains an admitted native route because this host exposes no supported completion signal. That route is intentionally retained until explicit operator review/stop; the tool reports `delivery_completion="unobservable"` rather than claiming a delivered message.
- Same-session watches use the existing GOWA-style `pre_gateway_dispatch` bridge: check the stored physical session against the current session, then call `ctx.inject_message(session_key=...)`. No Hermes core patch is required. A reset observed before injection stops forwarding; a concurrent `/new` between this check and the host lookup can still race. Injection acceptance means scheduled, not delivered; ambiguous injections are not replayed.
- Native new-session admission (`HTTP 202` or duplicate) proves admission only; it does not prove model execution or external delivery.

Operator setup, required Google APIs/scopes, Pub/Sub IAM, callback TLS, the worker service, and exact limitations are in [docs/watch-setup.md](docs/watch-setup.md). The isolated test suite uses a deterministic fake Google transport; it is not proof of live Google behavior.

## Security and limitations

- `gws auth login` 0.22.5 uses a random loopback callback without PKCE or `state`. The plugin correlates the live process, exact port, profile, chat, and sender; this mitigates confusion but does not replace PKCE.
- Renewal never trusts the browser alone: the new staging directory is swapped into `accounts/<account>/` only after native `gws` exits successfully, writes `credentials.enc`, and reports the same Google identity. A profile-wide swap lock plus a stable per-account lock serialize commits and native readers: different accounts may run in parallel, the same account serializes, and a swap waits for all readers. A failed replacement restores the previous directory when possible. The two renames are not crash-atomic: a process crash or failed rollback can leave a hidden `.ACCOUNT.backup-*` directory requiring local recovery. Avoid independently invoked native commands during renewal; external `gws` processes do not take this lock.
- Live health checks and error classification are allowlisted summaries. Health checks never return raw native stdout/stderr. Native `gws` owns decryption and Workspace token refresh. Exception: the Gmail Pub/Sub worker captures account-scoped `gws auth export --unmasked` into memory and refreshes an access token against Google for pull/ack only; it never logs or persists exported credentials or its access-token cache.
- The pasted callback is not end-to-end secret on a messaging platform. The plugin rewrites recognized callbacks and attempts best-effort deletion, but platform servers, notifications, devices, and participants in a shared chat may retain or see copies. Choose the chat accordingly. Never send the OAuth client JSON, exported credentials, or refresh tokens through chat.
- Paste the callback before the five-minute deadline. After expiry the temporary hook is gone and Hermes may receive the message normally.
- Login completion works only in the process and profile that started it: the same chat and sender in gateway, or the same host-bound session identity in other frontends. Explicit callbacks are not redacted or removed by the plugin; they may reach the model provider, logs, and persisted history. API clients must preserve session identity and use the same worker process for both steps. There is no platform allowlist or denylist.
- Account separation is logical per Hermes profile, not an operating-system security boundary against other processes running as the same user. A native global OAuth client may be shared, but authorized account tokens are never shared by this plugin.
- Public API tools block native `auth` commands; use `gws_login` and `gws_accounts` for account lifecycle and health. A shared native Hermes approval gate covers remote mutations, destructive operations, sends/shares and local file input/output. Read-only queries and schema discovery do not require that extra approval. This is not an OS sandbox: other tools and processes remain governed by Hermes and host permissions.
- `--full` may exceed the scope limit of an unverified external application. Use `default`, `readonly`, or `custom` when necessary.

## Verify

From a checkout:

```bash
python -m unittest discover -s tests -v
hermes plugins doctor .
hermes plugins validate .
```

The offline suite uses a fake local `gws` process; it does not contact Google or prove a live OAuth authorization.

## Remove

Stop or review outstanding watches first, keep the worker running until owned channel/route cleanup completes, then stop its service before removing the plugin. Explicitly stopping an admitted route may cancel a reply still in flight. Operator-provisioned Pub/Sub resources are never deleted.

```bash
hermes plugins disable gws-oauth
hermes plugins remove gws-oauth
```

Plugin removal does not delete `$HERMES_HOME/gws-oauth/`. Remove that state separately only when you intentionally want to discard all authorized accounts and the OAuth client.

See [docs/implementation.md](docs/implementation.md) for the flow and security decisions.
