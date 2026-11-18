# Catalog policy disclosure and design decisions

## Not currently eligible for catalog admission

This release is independently distributed, **not approved by Nous Research or admitted to the Hermes community catalog**. A draft catalog proposal is a request for maintainer discussion, not an endorsement. Disclosure does not waive a policy requirement. Do not merge that proposal without resolving the blockers or an explicit upstream policy decision.

Policy reviewed against [Hermes catalog admission rules](https://github.com/NousResearch/hermes-agent/blob/05348a3094a2356b9a271a2f22cdd6711c9952b8/plugin-catalog/README.md). Automated validation passing does not establish compliance with every manual-review rule.

## Known conflict: rule 11, Hermes-managed credentials

The current design does **not** follow the required third-party `oauth:` declaration → `hermes auth add <plugin>:<id>` → `credentialed_provider_request` path:

- It reads a Google Desktop OAuth client JSON from the active profile or native `gws` configuration, and copies it into private login staging. It does not obtain that client through declared Hermes secret variables.
- `gws_login` starts native `gws auth login`, rather than Hermes-managed PKCE login. Native `gws` stores and refreshes credentials in per-account configuration directories. The plugin moves/replaces/removes those directories during login, renewal and local logout. These are external CLI credential stores, not Hermes-managed grants.
- Native Workspace commands perform authenticated requests themselves; Hermes does not attach their tokens through `credentialed_provider_request`.
- The Gmail Pub/Sub worker captures `gws auth export --unmasked` for the selected account and performs OAuth refresh against Google's token endpoint. Exported secrets and cached access tokens remain in worker memory, but that still violates the requirement that plugins never hold or refresh tokens themselves.
- Credentials remain outside Hermes `auth.json`, survive plugin removal, and require explicit account cleanup. Blocking `auth export` in agent-facing tools prevents that direct disclosure path; it does not make the underlying architecture compliant.

The reason for retaining this design is functionality, **not a claim that the policy is optional**: arbitrary account slots in one profile, native `gws` method/helper coverage and file transfer, and login from remote chat. In the inspected Hermes implementation, each manifest OAuth ID has one stored grant. Multiple fixed IDs can coexist, but there is no dynamic account selector for a single OAuth ID. The credentialed request API deliberately does not return tokens, accepts JSON request bodies but no raw/multipart upload body, and native OAuth login refuses gateway/agent execution. Merely moving files into `auth.json`, reading it directly, or changing the manifest at runtime is not a supported migration.

A compliant redesign should use supported named-account grants and suitable request/file-transfer APIs from Hermes, or explicitly reduce functionality to what those APIs support. No Hermes core modifications, monkeypatches, token extraction from Hermes, or policy bypasses are included in this release.

## Review warning: rule 9, public host surfaces

The plugin does not replace or rebind Hermes core functions. It does, however, import undocumented `gateway.session_context` helpers and `hermes_cli.webhook`, including private locked compare-and-swap route helpers. Validation reports private-surface warnings. The route code uses the host's locks and ownership/digest checks instead of writing `webhook_subscriptions.json` directly, but this is still a dependency on non-public APIs and needs maintainer review or a supported public replacement. A passing no-core-override check is not a stability promise.

## Rule 12: unattended login and approvals

Login is intentionally available from a chat tool: it returns a consent URL and maintains a bounded, five-minute pending attempt instead of waiting indefinitely for a human. Missing session identity fails closed, and expiration terminates the native child. This differs from Hermes-managed OAuth's terminal-only initiation and belongs in the rule-11 blocker, even though the custom path is bounded. It is not suitable for unattended cron authorization.

Mutations, sends, shares, uploads, local file input/output and logout request the native Hermes approval gate. The operator's configured approval mode still applies; the plugin neither supplies its own approval nor changes that mode. Reads do not require this extra gate. Watch start explicitly authorizes bounded background subscription creation, renewal, reads and cleanup; the worker does not prompt on every renewal. This is not an OS sandbox or protection from other authorized tools.

## Rule 13: operational and privacy disclosures

- **Network and content:** native `gws` calls Google Workspace and discovery/OAuth endpoints. The worker calls Google Pub/Sub/OAuth and the configured native Hermes webhook endpoint. Drive/Calendar require public HTTPS callback ingress to a loopback worker via an operator-managed proxy. Returned Workspace data enters the agent context and can reach the configured model provider; notifications forward matching metadata to the configured conversation. No independent analytics or telemetry is implemented by this plugin; upstream binaries and configured services have their own policies.
- **Files outside the installed plugin:** `$HERMES_HOME/gws-oauth/` holds client JSON, encrypted credentials, encryption keys, token caches, private working files, staging/backups and large response files. `$HERMES_HOME/plugin-data/gws-oauth/` holds watch state/locks. Native Hermes webhook helpers persist owned routes in the profile's webhook subscription store. A global native `gws` client file may be read. User-supplied native file flags remain subject to host filesystem permissions and approvals; the private working directory is not a chroot.
- **Execution and lifetime:** subprocesses execute the separately installed `gws` binary without a shell. Login uses temporary subprocesses, watcher threads and timers. Watches require a separately supervised long-running Python worker with profile write access. Installation does not start the worker, provision cloud IAM, or subscribe to Google resources. The source package bundles no native binary and has no self-updater; CI downloads the official checksum-pinned `gws` test binary.
- **Account trust:** one profile is one trusted operator. Every authorized session in that profile can use all connected accounts. This is neither multi-tenant authorization nor isolation from another process running as the same OS user.
- **Callback exposure:** native `gws` 0.22.5 login lacks PKCE and OAuth `state`. Process/port/session correlation does not replace those protections. Gateway interception is temporary and best effort, not a universal secret filter; non-gateway/explicit callbacks can enter model context and history. Chat platforms may retain copies even after deletion. Never paste client JSON or refresh tokens into chat.
- **Lifecycle limitations:** credential replacement uses recoverable renames, not a crash-atomic transaction. Independent native commands do not share the plugin locks. Logout is local only and refuses while watches still require credentials; Google consent is not revoked. Response files have no automatic retention and need explicit cleanup.
- **Delivery limitations:** same-session injection has a documented concurrent `/new` race; admission is not delivery confirmation. Native one-shot routes may remain until explicit stop/review because completion is unobservable. Push/history gaps, bounded metadata inspection and POSIX-only worker locking limit coverage. See [watch setup](watch-setup.md).

## Why the other choices remain

Curated tools cover common tasks; `gws_schema` and guarded `gws_api` avoid copying the entire upstream CLI. One shared executor and approval policy prevent divergence between surfaces. Full scope mode remains the explicit product default for user-owned OAuth clients, with narrower presets/custom scopes available; it is not a least-privilege claim and Google may reject broad consent for an unverified app. Gateway callback parsing is shared and bounded so safety fixes apply to both interception and explicit completion.

See [OAuth setup](oauth-setup.md), [login/logout](login.md), [tool policy](api-tools.md), [implementation](implementation.md), and [tested compatibility](compatibility.md) for the actual operating contract.
