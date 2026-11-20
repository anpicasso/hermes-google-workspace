# Native login, reauthorization and logout

The public plugin wraps **native gws**, which owns consent, credential storage and refresh. The wrapper selects the profile/account directory, isolates the process environment and coordinates lifecycle locks. It never reads, exports or refreshes Google tokens. One Hermes profile represents one trusted operator.

## Commands

Install/enable `google-workspace` first, then run these in a local terminal:

```bash
hermes gws login person@example.com --client-file /secure/path/desktop-client.json --readonly
hermes gws accounts
hermes gws status person@example.com
hermes gws logout person@example.com
```

- **login:** invokes `gws auth login` with native terminal/browser interaction in a fresh staging slot. It verifies the Google identity through native gws before activating the slot. Repeating login reauthorizes; cancellation, native failure or wrong identity preserves the old slot. The command has a ten-minute timeout. No browser callback is intercepted by the public plugin.
- **accounts:** lists this profile's stored native slots without a network call. A slot name alone does not prove identity or current validity.
- **status:** runs native `gws auth status`, returns sanitized health/scopes, and exits nonzero if validity cannot be confirmed. It does not print credentials or raw native output.
- **logout:** invokes native `gws auth logout` for only that account and checks that local credential files were cleared. It may leave the directory/client file. This is local logout, **not Google-side consent revocation**.

Use `hermes --profile <name> gws ...` for another profile. Account arguments are Google email addresses, normalized to lowercase. The wrapper sets the absolute `GOOGLE_WORKSPACE_CLI_CONFIG_DIR`, file keyring, disposable isolated HOME and dotenv-safe working directory automatically. Existing storage remains at `$HERMES_HOME/gws-oauth/accounts/<account>/`.

Login/logout refuse while that account has active watches or unfinished remote cleanup. Stop those watches and let the worker finish cleanup, then retry. Cooperative execution/swap locks protect normal API calls; you do not need to stop calls for unrelated accounts. Do not bypass these locks with concurrent manual native auth.

## Client and scopes

Create your own [Google Cloud Desktop OAuth client](oauth-setup.md). `--client-file` accepts that client configuration, **not** an exported account credential. Without the flag, login reuses the account's `client_secret.json`, then the profile's `gws-oauth/client_secret.json`. It never silently borrows global credentials or an unrelated Google account.

```bash
hermes gws login person@example.com --readonly
hermes gws login person@example.com --full
hermes gws login person@example.com --scopes 'email,https://www.googleapis.com/auth/gmail.readonly'
hermes gws login person@example.com --services gmail,drive,calendar
```

`--readonly`, `--full` and `--scopes` are mutually exclusive native options. With none, gws uses its native scope picker. `--services` filters that picker. `--full` includes Pub/Sub/cloud scopes; choose deliberately. Include an identity-capable grant (`email`/userinfo, or suitable Gmail scope) so native identity verification can succeed; Drive/Calendar-only grants without identity access are refused at activation. Consent does not guarantee all requested scopes were granted or APIs enabled.

Native login is interactive: complete it in a terminal with an appropriate browser. On a remote server, a browser's localhost is not the server's localhost; arrange a supported native loopback/tunnel workflow or use the optional private companion. Never paste callback URLs or client JSON into ordinary chat expecting the public wrapper to intercept them.

## Binary resolution

By default the plugin resolves `gws` on PATH. An explicit public setting selects the executable for the account CLI, compatible remote companion, tools and worker by default:

```yaml
plugins:
  entries:
    google-workspace:
      settings:
        gws_bin: /absolute/path/to/gws
```

An explicitly configured missing/non-executable binary fails; it does not silently fall back to another binary. Set a filename/path, not a shell command or flags. Paths containing spaces are supported. No binary is installed automatically.

## Recovery and boundaries

Normal login errors roll back without replacing the old slot. A crashed login can leave a leased directory under `.pending/`. On POSIX the next login sweeps abandoned leases without touching live ones; on Windows cleanup is owner-only. Interrupted swaps can leave an `accounts/.<account>.backup-*` directory: inspect it locally with no auth process running before cleanup; never copy token/cache/key files between identities. Backups are retained if cleanup fails. Same-user malicious filesystem replacement is not a supported isolation boundary.

Local logout also prevents pending logins begun before that logout from reactivating the slot. The optional private companion uses the registered `gws-native-account-v1` instance in the same profile; it has no separate binary setting or credential-store implementation.

The private `gws-remote-auth` companion supplies conversational `gws_login` start/status/cancel and approval-gated `gws_logout`; it is optional and separately installed. This public CLI is operator-side only, not a new agent tool or gateway slash command. `gws_api` still blocks native auth/export. Do not enable the old combined `gws-oauth` plugin alongside this plugin because tool names collide.
