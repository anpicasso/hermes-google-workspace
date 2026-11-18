# Login lifecycle

Configure your [Google Cloud Desktop OAuth client](oauth-setup.md) first. This flow uses native `gws`, not Hermes-managed OAuth; see the [catalog policy disclaimer](catalog-policy.md).

One Hermes profile is one trusted operator with any number of Google accounts.
The native `full` scope preset remains the default; use `readonly` or explicit
`custom` scopes when desired. The Google consent screen determines what the user
actually grants. Requested scope metadata is not proof of granted access; use
`gws_accounts(check_health=true)` for the sanitized native health/scope summary.

## Connect, check, cancel, renew

- `gws_login(account="person@example.com")` starts the browser flow.
- `gws_login(account="person@example.com", action="status")` checks the attempt
  without creating another one. A completed outcome is consumed once. After that,
  `state="none"` with `account_registered=true` means credentials are stored, not
  that their current validity has been confirmed.
- `gws_login(account="person@example.com", action="cancel")` abandons this
  session's pending attempt, preserving existing credentials. If it already
  committed, cancellation reports `committed=true`; it does not log the account out.
- `gws_login(account="person@example.com", reauthorize=true)` renews an account
  without deleting the old credentials first. Replacement occurs only after the
  returned Google identity matches. Cancellation and failed authorization preserve
  the old slot; a replacement failure attempts rollback.

All attempt operations bind to the same profile, frontend, session/chat, sender,
and exact account. On the Hermes host the browser callback completes automatically.
On a different device, localhost points to that device, not the Hermes host; the
returned instructions explain completion through the originating frontend.
Malformed callback input remains retryable within the original deadline.
Explicit Google denial ends the attempt with `access_denied`; other OAuth errors
use `oauth_error`. A restart loses the pending in-memory attempt: start again.

Preflight checks require a bounded, valid Desktop OAuth client JSON and an available
`gws` binary. Results identify only `client_source=profile|gws_global`, never client
IDs or secrets. On systems with advisory file locks, a new start can clean abandoned
staging directories only when they have a lease that is no longer held. Unmarked
or live staging directories are not swept; automatic sweeping is disabled without
that locking support.

## Log out locally

`gws_logout(account="person@example.com")` requests Hermes approval, cancels older
pending authorizations for that account, then removes only its local account slot.
Other accounts and the profile OAuth client remain intact. A durable account epoch
prevents an older authorization in another plugin process from reinstalling the
credentials after logout. A genuinely new login started after logout is allowed.

This is **not remote revocation**: `remote_revoked=false`. Google-side consent and
Google data remain unchanged. Revoke the application separately in Google Account
settings when that is the intended operation. Stop account watches and allow remote
cleanup to finish before logout; otherwise it returns `watches_active`. Do not start
new watches concurrently with logout. There is no force-delete shortcut that would
strand remote subscriptions without credentials to clean them up.

Native `gws auth logout` leaves its configuration directory behind. The plugin's
explicit removal handles that empty slot too, so a fresh login is not blocked by a
leftover directory. Prefer `reauthorize=true` for expired tokens rather than deleting
credentials as a routine repair.

## Why callback interception stays

An OAuth authorization code is short-lived and single-use. After a successful token
exchange it cannot be reused to obtain another token. But before exchange—or when
completion is uncertain—it is sensitive. A callback URL can also contain metadata
such as scopes or state, depending on the client. It is not an access or refresh token.

Gateway interception reduces exposure by consuming recognized callbacks before the
agent sees them. Removing it would expose still-live codes to the model/history,
not merely harmless used codes. It is not end-to-end secrecy: the chat platform,
notifications and shared-chat participants may retain copies; explicit tool callback
completion and non-gateway frontends can also expose URLs to model/history.
Do not paste callbacks after the attempt expires. Never paste client secrets,
exported credentials, refresh tokens or standalone codes.

The gateway hook and explicit completion share one bounded URL parser, endpoint
validation and exchange path. Plain `http://localhost:<port>/...` and the existing
Safari `localhost:<port>/...` form are supported (scheme/host case-insensitive).
The hook checks platform, chat, user and profile before inspecting a paste; an
unrelated port is left alone. Duplicate or mixed code/error fields, blank codes,
userinfo, fragments and embedded whitespace/control characters are rejected
without consuming the attempt. Parsing is capped at 8 KiB and 32 query fields.
Every recognized callback, including malformed pastes and exchange cancellation,
uses the same sanitized rewrite and best-effort platform deletion path.

This is deliberately **not a universal secret filter**: it does not extract URLs
from prose/Markdown, watch unrelated conversations, or intercept after its pending
login is removed or the process restarts. Paste only the plain callback while the
login is pending. Generalizing the shared validation avoids drift without adding
an always-on cross-plugin OAuth framework.

Reference: [OAuth 2.0 authorization-code rules, RFC 6749 §4.1.2](https://www.rfc-editor.org/rfc/rfc6749#section-4.1.2).
