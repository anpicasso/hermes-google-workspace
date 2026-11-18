# Google Cloud OAuth setup

The plugin needs **your own Google Cloud OAuth client of type Desktop app**. A Google API key or service-account JSON is not a substitute for this user login flow. The `gcloud` executable is optional: use the Cloud Console, or native `gws auth setup` if you already use `gcloud`.

This is native `gws` authentication, not Hermes-managed OAuth. Read the [catalog policy disclosure](catalog-policy.md) before installing.

## 1. Create the Google client

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or select a project you control.
2. Under **APIs & Services → Library**, enable the APIs you intend to use: Gmail API, Google Drive API and Google Calendar API for the curated tools; other Workspace APIs for corresponding generic calls. Gmail watches additionally need Cloud Pub/Sub API and IAM setup in [watch-setup.md](watch-setup.md).
3. Configure **Google Auth Platform** (the OAuth consent screen): app name, support/contact email, audience and data-access scopes. Internal audience is available only where your Workspace organization permits it. For an External app in **Testing**, add each Google account you will connect as a test user.
4. Under **Google Auth Platform → Clients**, choose **Create client → Desktop app**. Do not create a Web application client for this loopback flow. Download the JSON file and keep it private.
5. Use the same client for multiple accounts if desired. Each account still grants consent separately. An administrator may restrict scopes or third-party apps; owning the project does not override organization policy.

External Testing apps may receive refresh tokens that expire after seven days when requesting Workspace scopes. Unverified-app warnings, user/scope limits and verification requirements are Google's rules; broad `full` consent may be rejected. Use `readonly`, `default`, or a precise `custom` scope set rather than assuming every requested scope will be available.

References: [create OAuth credentials](https://developers.google.com/workspace/guides/create-credentials#oauth-client-id), [configure consent](https://developers.google.com/workspace/guides/configure-oauth-consent), [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

## 2. Put the file where login can find it

Client lookup order:

1. `$HERMES_HOME/gws-oauth/client_secret.json` — profile override.
2. The native `gws` client location reported by `gws auth status`, normally `~/.config/gws/client_secret.json`.

For the default profile on a POSIX host:

```bash
install -d -m 700 "$HOME/.hermes/gws-oauth"
install -m 600 /secure/path/downloaded-client.json \
  "$HOME/.hermes/gws-oauth/client_secret.json"
```

For a named profile, use its actual home, normally `$HOME/.hermes/profiles/<name>/gws-oauth/`. If `HERMES_HOME` is customized, use that home instead. Do not assume a shell variable changes just because another command uses `hermes --profile <name>`.

Alternatively, share the native client file:

```bash
install -d -m 700 "$HOME/.config/gws"
install -m 600 /secure/path/downloaded-client.json \
  "$HOME/.config/gws/client_secret.json"
```

The selected JSON must be a regular, non-symlink file with an `installed` object containing `client_id` and `client_secret`. Keep the file outside the installed plugin directory so updates do not replace it. On non-POSIX systems apply equivalent user-only access controls.

**Can the plugin configure it for me?** No chat upload/import tool is provided. Plugin activation creates the private profile directory; an operator must install the client file locally. The plugin does not create a Cloud project, enable APIs, or configure a consent screen. Never paste or attach the JSON to a chat.

## 3. Native `gws auth` is still usable

If native `gws` is already configured with a client JSON, the plugin can reuse that client without another copy. **It does not automatically adopt the account logged into global `gws`.** Plugin-authorized accounts have separate profile-local directories and must be connected through `gws_login`.

Native terminal commands remain available to the operator:

```bash
gws auth setup --project YOUR_PROJECT_ID  # setup helper; requires gcloud
gws auth login --readonly               # native global account login
gws auth status                         # inspect locally; do not post raw output
```

`gws auth setup` assists native setup; follow its instructions and verify the resulting client file. It is not a plugin command and does not replace Google consent. Native `gws auth login` works once the native client is configured. Native global logout does not remove plugin account slots.

The agent-facing `gws_api` deliberately blocks the entire native `auth` namespace, including export. That is a tool boundary, not a ban on operator use of the CLI. Do not run native auth/logout commands against a plugin-owned account directory during renewal or while watches depend on it; use [the plugin lifecycle](login.md).

Inherited `GOOGLE_WORKSPACE_CLI_*` and `GOOGLE_APPLICATION_CREDENTIALS` overrides are cleared for account-bound plugin calls. Environment-only native client configuration is therefore insufficient: install one of the JSON files above. Account credentials, encryption keys and token caches live under `$HERMES_HOME/gws-oauth/accounts/<account>/`, with native file-keyring storage. The encryption key is on the same host; this does not protect against processes running as the same OS user.

## 4. Connect and verify

Ask Hermes to connect an exact account, choosing a scope mode. For example:

```json
{"tool":"gws_login","arguments":{"account":"person@example.com","scope_mode":"readonly"}}
{"tool":"gws_login","arguments":{"account":"person@example.com","action":"status"}}
{"tool":"gws_accounts","arguments":{"check_health":true}}
```

Without an explicit scope mode, `full` is the default. A browser on the Hermes host completes through loopback; on another device follow the tool's instructions and paste only the plain callback URL into the same session while the attempt is pending. Prefer a private conversation. See [login/logout and callback privacy](login.md).

A stored account is not proof of current authorization. The health check reports a sanitized validity/scope summary; use a bounded service read to verify the API and permissions you need. For custom scopes include the required Workspace scopes and, for Gmail watches, the Pub/Sub scope and IAM permissions. Reauthorize with `reauthorize=true` to change consent without deleting the old credential first.

## Common setup failures

- `client_missing`: neither client file exists; install one locally.
- `client_invalid`: invalid/oversized JSON, Web rather than Desktop client, missing required fields, or unreadable file. Do not paste the file to diagnose it.
- `gws_not_found`: install the CLI or configure `plugins.entries.gws-oauth.settings.gws_bin` to its absolute path.
- Google denies access: check test-user membership, audience, administrator policy and requested scopes.
- API permission failure after successful login: enable the API and check granted scopes; Pub/Sub also requires IAM. Blind reauthorization does not fix missing IAM.
- Expired/revoked authorization: use `gws_login(..., reauthorize=true)` and complete fresh consent. Local logout is not required for routine renewal.
