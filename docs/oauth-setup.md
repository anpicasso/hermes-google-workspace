# Google Cloud OAuth setup

The public adapter uses native **gws authentication**, not Hermes-managed OAuth. You need your own **Google Cloud OAuth client of type Desktop app**. An API key or service-account JSON is not a substitute. `hermes gws setup EMAIL` launches the native interactive wizard and requires `gcloud`; if you provision a Desktop client yourself, native login does not require `gcloud`.

## Create a client

1. Create or select a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the Workspace APIs you need: Gmail, Drive and/or Calendar. Gmail push additionally needs Pub/Sub and IAM setup; see [watch setup](watch-setup.md).
3. Configure Google Auth Platform: branding, audience and data-access scopes. For External apps in Testing, add every Google account as a test user. Organization policy may restrict consent.
4. Create a **Desktop app** OAuth client and download its JSON. Do not use a Web application client for loopback login.
5. One client may authorize several accounts; each account consents independently.

External Testing apps requesting Workspace scopes may receive refresh tokens that expire after seven days. Google verification rules, scope limits and administrator policy still apply. Requested scopes do not prove granted access.

References: [create credentials](https://developers.google.com/workspace/guides/create-credentials#oauth-client-id), [configure consent](https://developers.google.com/workspace/guides/configure-oauth-consent), [token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

## Store the downloaded client

Use `<actual Hermes profile home>/gws-oauth/accounts/<lowercase-account>/client_secret.json`. This stable directory is created automatically by the account wrapper. Set user-only permissions (directory 0700/file 0600 on POSIX); use real directories and regular files, not symlinks/hardlinks. Keep credentials outside the installed plugin snapshot.

The public adapter selects that store using `GOOGLE_WORKSPACE_CLI_CONFIG_DIR`; gws owns its credentials, key and token cache. It does not copy a global/profile client or adopt the globally logged-in account. Provision each account explicitly using the complete [native login/logout instructions](login.md).

## Native auth and optional companion

`hermes gws setup EMAIL` runs the native wizard, which can sign into gcloud, create/select a project, enable APIs, and prompt for the Desktop client ID and secret from Cloud Console. Enter those only in the local native terminal. The wizard may also offer login; its result is checked before routing. With an existing native client in this slot, skip the wizard and run `hermes gws login EMAIL`.

The optional `gws-remote-auth` companion supplies conversational login/logout and cross-device callbacks **after** the same native setup. It has no profile/global client fallback. The public plugin works without it. Never paste client JSON, exported credentials or tokens into chat.

## Verify and troubleshoot

- `gws_accounts(check_health=true)` returns sanitized native validity and scope information. A slot label or stored file alone does not prove the Google identity; verify the native user matches and perform a bounded service read.
- Missing CLI: install gws or configure `plugins.entries.gws-workspace.settings.gws_bin` with its absolute executable path.
- Consent denied: check test users, audience, requested scopes and organization policy.
- API denied after login: check API enablement and granted scopes. Reauthorization does not fix missing IAM.
- Expired/revoked credentials: stop watches and complete cleanup before native reauthorization, or use the companion's guarded flow.

Native calls strip inherited raw tokens/ADC and use isolated HOME plus a controlled working directory with an empty `.env` sentinel. Do not populate the isolated HOME with unrelated gcloud credentials. The native file-keyring key is on the same host: it cannot protect against a hostile process running as your OS user.
