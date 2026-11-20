# Hermes Google Workspace

Multi-account Google Workspace access through the [native `gws` CLI](https://github.com/googleworkspace/cli): curated Gmail, Drive and Calendar tools, guarded advanced commands, and targeted change notifications.

**Native `gws` owns Google credentials, decryption and refresh.** This public plugin does not implement OAuth login, export tokens, or require the optional private remote-login companion. One Hermes profile belongs to one trusted operator; all authorized sessions in that profile can use its connected accounts.

## Install and configure

Requirements: Hermes with the plugin APIs and bound-context compatibility adapter described in [compatibility](docs/compatibility.md), native `gws` (tested baseline 0.22.5), and the Python dependencies declared in `plugin.yaml` (PyYAML, aiohttp and google-auth with requests). Watches require POSIX locking and a separately supervised worker. No binary, cloud provisioning or worker auto-start is included.

Install the v0.9.0 release:

```bash
hermes plugins install anpicasso/hermes-google-workspace --ref v0.9.0 --enable
hermes config set plugins.entries.google-workspace.settings.gws_bin /absolute/path/to/gws
```

Use `hermes --profile <name>` for another profile. Check the installer result for live reload; a fresh session may be needed for updated tools, and older frontends may require an external process reload.

### Google OAuth and accounts

You need your own **Google Cloud Desktop app OAuth client**, not an API key. The `gcloud` executable is optional for login. [Create the client, enable APIs and configure consent](docs/oauth-setup.md), then use [native per-account login/logout](docs/login.md):

```bash
hermes gws setup person@example.com           # interactive gcloud / Desktop client wizard
hermes gws login person@example.com --readonly
hermes gws accounts
hermes gws status person@example.com
hermes gws logout person@example.com
```

These local commands handle account directories and isolation automatically; native gws still performs OAuth. `gws` is resolved on PATH unless `plugins.entries.google-workspace.settings.gws_bin` sets an explicit executable. Setup/login/logout refuse active watches or pending cleanup. Setup requires `gcloud` and operator input in Cloud Console; skip it if this slot already has a Desktop client. Login writes in place: a failed or wrong-account attempt is blocked from routing, not rolled back.

Each account has a native configuration directory:

```text
$HERMES_HOME/gws-oauth/accounts/<account>/
```

The legacy `gws-oauth` storage name is deliberately retained; renaming the plugin must not silently move or lose existing accounts. Account selection chooses that directory, not an exported token. Global native accounts, raw-token environment variables and global Application Default Credentials are not automatically adopted. Native `gws auth` remains usable by the operator; the agent-facing `gws_api` blocks the auth namespace.

### Optional remote login

The separately installed private [hermes-gws-remote-auth](https://github.com/anpicasso/hermes-gws-remote-auth) companion (`gws-remote-auth`) adds chat-based `gws_login` and approval-gated local `gws_logout`. It shares this plugin's profile-bound account backend and `gws_bin` setting. Repository access is restricted; it is not bundled, listed in the catalog, or required for public operation. Without it, use the terminal commands above. Never paste client JSON or refresh tokens into chat.

## Tools

Load the bundled operational skill with `skill_view("google-workspace:workspace")`; it covers accounts, agent-driven watches and public HTTPS ingress options.

The public plugin registers **18 tools**:

- Accounts: `gws_accounts` (list or sanitized native health).
- Gmail: `gws_gmail_search`, `gws_gmail_read`, `gws_gmail_draft`, `gws_gmail_send`.
- Drive: `gws_drive_search`, `gws_drive_read`, `gws_drive_upload`, `gws_drive_share`.
- Calendar: `gws_calendar_list`, `gws_calendar_freebusy`, `gws_calendar_create`, `gws_calendar_update`.
- Discovery/advanced: `gws_schema`, `gws_api`.
- Notifications: `gws_gmail_watch`, `gws_drive_watch`, `gws_calendar_watch`.

Every Workspace operation selects an explicit account. Known reads run directly; mutations, sends/shares, local file input/output and unknown methods/options request the native Hermes approval gate. The configured host approval mode still applies. A model-supplied `approved` field cannot grant permission.

```json
{"tool":"gws_accounts","arguments":{"check_health":true}}
```

```json
{"tool":"gws_gmail_search","arguments":{"account":"person@example.com","query":"is:unread"}}
```

[Tool mappings and examples](docs/api-tools.md). Relative files resolve under `$HERMES_HOME/gws-oauth/files/`, not the gateway's working directory. Absolute paths retain their meaning. The CLI is not a filesystem sandbox. Large successful output is saved privately as `output_file`; process and delete it after use. Raw native failures are sanitized; successful results can contain private Workspace content.

## Notifications

```text
Gmail users.watch → authenticated Pub/Sub HTTPS push ┐
Drive changes.watch → HTTPS callback                 ├→ durable hints/cursors
Calendar events.watch → HTTPS callback               ┘  → local filters → outbox → Hermes
```

Each tool supports `start`, `status`, `stop`, a bounded lifetime, and `same_session` or `new_session` delivery. The agent starts watches directly: the plugin captures the current bound runtime session and actor automatically, with no manual bind command or model-selected destination. Missing context, mixed/cleared actors and delegated children fail closed. This small internal-host compatibility exception is documented in [the route contract](docs/routes-contract.md).

The operator provisions delivery routes and public HTTPS ingress; one separately supervised worker per profile owns renewal, reconciliation and cleanup. Gmail push uses verified Google OIDC identity, exact audience/subscription/mailbox checks and durable acceptance before acknowledgement. Google API reads and renewals remain short account-locked native CLI calls. We do not run a persistent native watch alongside calls sharing its credential cache.

[Watch setup and limitations](docs/watch-setup.md). Same-session injection retains a concurrent `/new` race because the host has no atomic physical-session fence. Webhook admission is not proof of agent execution or delivery.

## Migration, security and removal

Disable the old `gws-oauth` plugin before enabling this one; duplicate tool names must not coexist. Preserve account directories and stop/review existing watches before switching route configuration. Move operator settings to `plugins.entries.google-workspace.settings`; do not copy credentials into the installed plugin directory. The private companion is installed separately if wanted.

Before removal, stop watches, allow remote cleanup, then stop the worker. Operator-provisioned Pub/Sub and Hermes routes are never deleted by this plugin. Removing the plugin does not remove credentials, watch state, or response files.

```bash
hermes plugins disable google-workspace
hermes plugins remove google-workspace
```

[Security and catalog disclosure](docs/catalog-policy.md): external native credential ownership, content egress, account trust, filesystem and delivery limits. No catalog acceptance or endorsement is implied.

## Verify

```bash
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --json
```

Offline transport/host integration tests are not live Google OAuth or delivery evidence. See [compatibility](docs/compatibility.md), [implementation](docs/implementation.md), [OAuth setup](docs/oauth-setup.md), [login/logout](docs/login.md), and [watch operations](docs/watch-setup.md).
