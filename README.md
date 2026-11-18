# Hermes GWS OAuth

Multi-account Google Workspace access for Hermes: curated Gmail/Drive/Calendar tools, guarded [native `gws`](https://github.com/googleworkspace/cli) commands, and targeted change notifications.

> **Catalog policy disclaimer:** this release uses native `gws` credential files and OAuth refresh rather than Hermes-managed OAuth. The Pub/Sub worker also holds exported credentials and refreshes tokens in memory. This conflicts with current catalog rule 11. It also relies on private host helpers flagged by validation. It is **not Nous-approved or catalog-admitted**; any draft proposal is for discussion, not an assertion of eligibility. Read the full [policy conflicts, design decisions and risk disclosure](docs/catalog-policy.md).

## What this plugin does

- Connect multiple Google accounts belonging to **one trusted operator per Hermes profile**. Every authorized session in the profile can use every connected account; use separate profiles for different operators.
- Search/read/draft/send Gmail, search/read/upload/share Drive files, and list/check/create/update Calendar events through curated tools.
- Use `gws_schema` and approval-gated `gws_api` for native operations not covered by curated tools. This hybrid avoids duplicating the entire upstream CLI.
- Watch specific mail conversations, Drive files/folders, and Calendar changes. Deterministic filters run before an agent is invoked. Watches need separate infrastructure and an explicitly supervised worker.

No Hermes core patch, automatic worker start, cloud provisioning or self-updater is included.

## Requirements and installation

- Hermes Agent `>=0.23.2`; version numbers alone do not establish compatibility. See the [tested revisions and CI](docs/compatibility.md).
- Native `gws` on PATH (tested: **0.22.5**). Install from [official releases](https://github.com/googleworkspace/cli/releases), or `npm install -g @googleworkspace/cli@0.22.5` with a compatible Node runtime. The plugin does not install the binary.
- A Google account and **your own Google Cloud Desktop OAuth client**. The `gcloud` executable is optional, not an authentication requirement.
- Runtime dependencies declared in `plugin.yaml`: PyYAML and aiohttp. Watches require a dependency-complete Python environment and POSIX locking; the tested service setup is Linux.

```bash
hermes plugins install anpicasso/hermes-gws-oauth --ref v0.8.0 --enable
# Or target a named profile:
hermes --profile <name> plugins install anpicasso/hermes-gws-oauth --ref v0.8.0 --enable
```

For immutable deployment, use the full commit SHA referenced by the release instead of a tag. Check the install result for live plugin reload; start a fresh session for updated tool schemas. If your host retains old Python handlers, reload the frontend process externally. Do not assume every host reloads plugins identically.

If the gateway cannot find `gws`:

```bash
hermes config set plugins.entries.gws-oauth.settings.gws_bin /absolute/path/to/gws
```

### Configure Google OAuth first

[Follow the complete setup guide](docs/oauth-setup.md): create/select a Google Cloud project, enable the required APIs, configure Google Auth Platform consent/audience/test users, create a **Desktop app** OAuth client, and download its JSON.

Install it locally in one of these locations:

```text
$HERMES_HOME/gws-oauth/client_secret.json  # profile override; wins
~/.config/gws/client_secret.json          # native gws fallback
```

Native `gws auth status` reports the exact fallback `client_config` path on systems using another location. On POSIX, use directory mode `0700` and file mode `0600`; the selected client must not be a symlink. Default profile home is normally `~/.hermes`; named homes are normally `~/.hermes/profiles/<name>`.

**The plugin does not accept client JSON through chat or create it for you.** Activation creates the private profile directory, but you install the JSON locally. Never commit it or put it inside the plugin directory.

**Already using native `gws auth`?** Its configured client JSON is reusable. Its globally logged-in account is not automatically imported: connect each account through the plugin. Operator terminal commands `gws auth setup` (requires `gcloud`), `gws auth login`, and `gws auth status` remain usable. Only the agent-facing `gws_api` blocks native `auth` commands. Environment-only Google credentials are not inherited by plugin calls. [Details and troubleshooting](docs/oauth-setup.md).

## Login, renewal and logout

Ask Hermes: “Connect `person@example.com` with read-only access.” Tool-call examples:

```json
{"tool":"gws_login","arguments":{"account":"person@example.com","scope_mode":"readonly"}}
{"tool":"gws_login","arguments":{"account":"person@example.com","action":"status"}}
{"tool":"gws_login","arguments":{"account":"person@example.com","action":"cancel"}}
{"tool":"gws_login","arguments":{"account":"person@example.com","reauthorize":true}}
{"tool":"gws_accounts","arguments":{"check_health":true}}
{"tool":"gws_logout","arguments":{"account":"person@example.com"}}
```

- Scope modes are `full` (**default**), `default`, `readonly`, or `custom` with explicit scopes. Full consent is a product choice, not least privilege; unverified apps or organization policies may reject it. Requested scopes are not proof of granted access.
- A browser on the Hermes host completes through the native loopback automatically. From another device, paste only the complete plain callback URL in the same session before the five-minute deadline; Safari's `localhost:...` form is accepted.
- Status checks do not start a new login; the finished outcome is consumed once. Cancel preserves existing credentials. Renewal keeps them until the replacement identity is verified; failed swaps attempt rollback, but recovery is not crash-atomic.
- `gws_accounts(check_health=true)` distinguishes stored credentials from native-confirmed validity and returns sanitized scope information, not tokens.
- `gws_logout` uses native Hermes approval and removes exactly one **local** account slot. It does not revoke Google consent or delete Google data. Stop watches and finish cleanup first; otherwise logout refuses with `watches_active`.

Gateway interception reduces callback exposure but cannot erase platform copies. Explicit completion/non-gateway callbacks can reach the model and history. Native `gws` 0.22.5 login lacks PKCE/state; session/port binding is not a replacement. [Full lifecycle, privacy boundaries and parser behavior](docs/login.md).

## Tooling and approval policy

The plugin registers **20 tools**:

- Lifecycle: `gws_login`, `gws_logout`, `gws_accounts`.
- Gmail: `gws_gmail_search`, `gws_gmail_read`, `gws_gmail_draft`, `gws_gmail_send`.
- Drive: `gws_drive_search`, `gws_drive_read`, `gws_drive_upload`, `gws_drive_share`.
- Calendar: `gws_calendar_list`, `gws_calendar_freebusy`, `gws_calendar_create`, `gws_calendar_update`.
- Discovery/advanced: `gws_schema`, `gws_api`.
- Notifications: `gws_gmail_watch`, `gws_drive_watch`, `gws_calendar_watch`.

Every Workspace operation selects an explicit account. Known reads run without the additional gate; mutations, sends/shares, uploads, file input/output, logout and unknown native methods/options request **Hermes approval**. The host's configured approval mode determines whether a human prompt is needed. A model-supplied confirmation cannot grant approval.

[Tool mappings, examples, file paths and approval semantics](docs/api-tools.md). Curated file paths resolve under `$HERMES_HOME/gws-oauth/files/`. The generic native CLI is not a filesystem sandbox. Successful output above 200,000 characters is saved privately under `gws-oauth/response-*` and returned as `output_file`; process and delete it locally. Responses are buffered, not streamed. Native failure output is sanitized; successful data can contain private Workspace content.

## Change notifications

```text
Gmail users.watch → Pub/Sub pull ───────┐
Drive changes.watch → HTTPS callback ──┼→ durable hints/cursors → local filters
Calendar events.watch → HTTPS callback ┘  → outbox → native Hermes delivery
```

Each watch offers `start`, `status`, `stop`, a bounded lifetime and `same_session` or `new_session` delivery. Gmail requires operator-provisioned Pub/Sub and IAM; Drive/Calendar require public HTTPS callback forwarding. One separate worker per profile owns renewal, reconciliation and cleanup; nothing starts automatically.

Same-session injection has a residual concurrent `/new` race and requires explicitly permitted in-process gateway injection. Native webhook admission is not delivery confirmation; one-shot routes can remain until explicit stop/review. [Setup, supported selectors, worker commands and limitations](docs/watch-setup.md).

## Security, storage and removal

Credentials and encryption keys live under `$HERMES_HOME/gws-oauth/accounts/<account>/`; the native client file may be shared, but account credentials remain profile-scoped. The Pub/Sub worker's exported secrets stay in memory. This is not isolation from another OS process running as the same user. Never paste client JSON, exported credentials or refresh tokens into chat.

Before removing the plugin, stop/review watches and let the worker finish owned channel/route cleanup, then stop its service. Stopping an admitted route can cancel an in-flight reply. Operator-provisioned Pub/Sub resources are not deleted.

```bash
hermes plugins disable gws-oauth
hermes plugins remove gws-oauth
```

Removal does not erase `$HERMES_HOME/gws-oauth/` or all watch state. Discard those separately only when intended; use account logout before removal where possible. [Complete disclosure](docs/catalog-policy.md) covers network/content egress, subprocesses, stored files, credential policy conflicts and non-public host dependencies.

## Verification and further documentation

```bash
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate . --json
```

The [CI instructions](docs/compatibility.md) run the complete suite with native Hermes and `gws` available and reject skipped tests. Offline tests are not proof of real Google authorization; release verification records distinguish offline checks from separately authorized live tests. Passing Doctor/Validate does not waive catalog policy.

- [Google Cloud OAuth configuration and native CLI reuse](docs/oauth-setup.md)
- [Login, cancel, renewal, logout and callback privacy](docs/login.md)
- [Tools and approvals](docs/api-tools.md)
- [Watch setup and operations](docs/watch-setup.md)
- [Implementation and recovery boundaries](docs/implementation.md)
- [Compatibility and verification](docs/compatibility.md)
- [Catalog policy disclaimer and decisions](docs/catalog-policy.md)
