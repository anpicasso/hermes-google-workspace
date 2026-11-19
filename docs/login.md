# Native login, reauthorization and logout

The public `google-workspace` plugin does not implement OAuth. **Native gws owns consent, credential storage and refresh.** Configure a [Google Cloud Desktop OAuth client](oauth-setup.md) first. One Hermes profile represents one trusted operator; its account slots are not independent tenants.

## Provision one account (POSIX)

Stop the watch worker and avoid concurrent plugin calls while provisioning or changing credentials manually. Stop existing account watches and let remote cleanup finish **before** stopping the worker. Direct terminal auth does not participate in the companion lifecycle guard.

Use the actual profile home, not an assumed shell `HERMES_HOME`. Replace the example values:

```bash
PROFILE_HOME="$HOME/.hermes"
ACCOUNT="person@example.com"
SLOT="$PROFILE_HOME/gws-oauth/accounts/$ACCOUNT"
GWS_BIN="$(command -v gws)"
install -d -m 700 "$SLOT" "$SLOT/.home"
install -m 600 /secure/path/downloaded-client.json "$SLOT/client_secret.json"
AUTH_CWD="$(mktemp -d)"
touch "$AUTH_CWD/.env"
(
  cd "$AUTH_CWD" || exit 1
  env -i PATH="$PATH" HOME="$SLOT/.home" \
    GOOGLE_WORKSPACE_CLI_CONFIG_DIR="$SLOT" \
    GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file \
    "$GWS_BIN" auth login --readonly
)
rm "$AUTH_CWD/.env"
rmdir "$AUTH_CWD"
```

Use an absolute `PROFILE_HOME`, lowercase account spelling, and an absolute `GWS_BIN`. The disposable working directory and empty `.env` prevent native dotenv ancestor discovery. Do not place ADC or unrelated config under the isolated HOME. On Windows, use equivalent user-only ACLs and an explicit environment including the required system variables.

`--readonly` is an example, not sufficient for every watch or write operation. Consult `gws auth login --help` for native presets/custom scopes. Gmail watch requires an appropriate Gmail scope; Pub/Sub push uses the operator's IAM configuration, not a Python-exported mailbox token. Drive/Calendar watch setup may require broader access than ordinary reads.

Complete consent using a browser on the same host, or a supported native remote-login method. If the browser is elsewhere, its localhost is not the Hermes machine. The public plugin does not intercept callback pastes; do not paste them into an ordinary chat expecting it to finish login.

Repeat with another account and another `SLOT` for multiple accounts. Do not copy credential/cache/key files between slots. Logging in to global gws does not automatically register an account here.

## Verify identity and access

In the same isolated environment, run native `gws auth status` and inspect its `user` identity locally; do not post raw output. Confirm it matches the slot account. If native status cannot confirm identity, obtain appropriate identity scope or verify with a service identity endpoint such as Gmail `users.getProfile` when authorized. A valid token or directory name alone is not identity proof.

Then use `gws_accounts(account="person@example.com", check_health=true)` and a bounded curated read. No credentials are returned by the health tool. Successful login does not guarantee API enablement or all requested scopes.

## Reauthorize or log out

First stop all watches for that account and wait for cleanup. Pause the worker and other calls. Reuse the isolated environment above, replacing `auth login --readonly` with the intended native login command or `auth logout`. Do not run auth against a shared store concurrently with plugin calls.

Native logout clears native credentials/cache; it may leave the directory/client file. It is **local logout, not Google-side consent revocation**. Revoke the app separately in Google Account settings if needed. Other account directories remain independent.

## Optional conversational login

The private `gws-remote-auth` companion adds `gws_login` start/status/cancel/reauthorize and approval-gated `gws_logout`. It coordinates account replacement/removal with public watches through a versioned lifecycle query and shared account lock. It is optional, separately installed, and not part of this public release. Follow its own callback privacy and setup instructions.

Without that companion, asking the agent to log in returns native operator guidance rather than handling credentials in Python. Do not enable the old combined `gws-oauth` plugin alongside the renamed public plugin: their tool names overlap.
