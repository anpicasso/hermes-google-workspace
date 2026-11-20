# Set up and use a native Google account

Run these commands in a POSIX operator terminal. Native auth writers inherit a process lock; managed writes fail closed on platforms without that guarantee. Add `--profile NAME` immediately after `hermes` for another profile. Each account gets its own native config directory automatically; you do not set environment variables or import credentials into Hermes.

```sh
hermes gws setup person@example.com
hermes gws login person@example.com --readonly
hermes gws accounts
hermes gws status person@example.com
hermes gws logout person@example.com
```

**First-time setup is interactive, not automatic.** `setup` runs `gws auth setup` in the selected account directory. Native gws 0.22.5 needs the `gcloud` CLI on PATH, a Google Cloud project with appropriate permissions, and a Desktop app OAuth client. Its wizard may sign you into gcloud, create/select a project, enable APIs, and ask you to create the client in Cloud Console and enter its ID and secret **in the native terminal**. It cannot create the Desktop client for you. Its disposable HOME does not reuse your global gcloud login and is cleaned up on normal exit. A killed supervisor may leave a private execution directory on disk; later commands never reuse it. The native account config directory persists. Review the project and changes before approving them. At the final login prompt choose **n** to run `hermes gws login` with explicit scopes; if you log in from the wizard, the same identity check applies.

Already have a working native client in the selected slot? Skip setup. Alternatively an operator can save a downloaded Desktop client as `client_secret.json` directly in the account directory printed by the wrapper, then run login; no gcloud is required for this route. Do not use symlinks, send client JSON through chat, or copy credential/key/cache files between accounts. The plugin does not accept `--client-file`, parse client secrets, or copy client files from global/profile defaults. See [Google Cloud setup](oauth-setup.md).

## Commands and outcomes

- **login** runs `gws auth login` directly in `$HERMES_HOME/gws-oauth/accounts/<lowercase-email>/`. `--readonly`, `--full` and `--scopes LIST` are mutually exclusive. `--services gmail,drive,calendar` filters the native picker. `--full` includes cloud scopes. Native gws owns consent, credential storage and refresh. The wrapper uses a ten-minute subprocess timeout and verifies the saved Google identity (native userinfo or a suitable Gmail profile grant) before allowing Workspace calls. Granted scopes can differ from requested scopes.
- **accounts** lists routable stored slots without a network call. A slot name alone is not an identity or validity check. **status** reports sanitized native health/scopes or that the slot is blocked. It exits nonzero unless validity is confirmed.
- **logout** asks native `gws auth logout` to clear only this account's local credentials and caches and checks the result. The directory, encryption key and client configuration may remain. If a credential file still exists, including an interrupted native `credentials.tmp` that gws 0.22.5 logout does not remove, it returns `logout_unconfirmed` and leaves the slot blocked. This does **not** revoke Google consent or delete Google data.

The executable defaults to `gws` on PATH. To override it set `plugins.entries.google-workspace.settings.gws_bin` to an executable path, not a command line. A missing override does not fall back. The same setting applies to the optional companion; it has no binary setting of its own.

## Concurrent use and recovery

Stop active/matched watches and finish remote cleanup before setup, login or logout. While an account is being changed, competing auth/watch operations wait briefly or refuse. Other accounts are independent. Cancel an active login at its originating terminal/session before logging out elsewhere. Do not run unmanaged native auth concurrently; those commands do not take these locks.

**There is no plugin rollback.** Before native setup/login changes a slot, the wrapper drains existing calls and marks the slot unverified. A wrong identity, cancellation, timeout, crash or unconfirmed result can leave new or old native credentials on disk. The slot is excluded from listing and both API/worker routing until a verified login; do not remove the marker manually. Retry with the exact account, or explicitly log out. Native login does not clear an old API token cache or fallback plaintext credential. If the wrapper reports `logout_required`, explicitly log out before retrying; it never silently deletes those files. Do not mix old and new workers/plugins during an upgrade. Existing operator-provisioned slots keep their paths and trust; legacy pending/backup directories are not migrated or cleaned up.

Each native command receives a disposable HOME, file keyring, explicit config directory and controlled working directory; inherited raw tokens, client secrets, ADC and ancestor dotenv credentials are not used. This is cooperative isolation for a trusted single-operator profile, not a sandbox against other processes under the same UID.

On a remote host the browser's localhost may not reach native gws. Use a supported loopback/tunnel workflow or the optional `gws-remote-auth` companion for **login after client setup**. The public plugin has no callback interceptor or agent login tool; `gws_api` blocks native auth/export.
