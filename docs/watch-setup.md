# Targeted Workspace watches: operator setup

Read [OAuth setup](oauth-setup.md) and the [catalog policy disclosure](catalog-policy.md): native credential storage and the Pub/Sub export/refresh path are known catalog blockers, not Hermes-managed OAuth.

This guide covers the `gws_gmail_watch`, `gws_drive_watch` and `gws_calendar_watch` tools added in 0.7.0. The tools use the same profile-local `gws` accounts as `gws_login`/`gws_api`; they do not implement a second OAuth flow.

## What runs where

- **Agent tools** validate strict arguments, resolve the trusted conversation origin, create the owned Hermes webhook route, and write one durable watch row. They make a small read-only Google call to establish a baseline.
- **One supervised worker per profile** (`watch_worker.py`) owns Google subscriptions, Pub/Sub pull, Drive/Calendar callbacks, cursor readers, renewal, cleanup, and forwarding. Plugin registration/Doctor/import never starts it.
- **Native Hermes webhook routes** carry each `new_session` notification; the native webhook platform owns the fresh session, admission, execution and response delivery.
- Same-session watches use the existing GOWA-style `pre_gateway_dispatch` bridge: check the stored physical session against the current session, then call `ctx.inject_message(session_key=...)`. No Hermes core patch is required. A reset observed before injection stops forwarding; a concurrent `/new` between this check and the host lookup can still race. Injection acceptance means scheduled, not delivered; ambiguous injections are not replayed.

Everything is stored under:

```text
$HERMES_HOME/plugin-data/gws-oauth/watch.sqlite3   # 0700 directory, 0600 database
$HERMES_HOME/webhook_subscriptions.json            # native route secrets, 0600
```

The worker uses the exact `--home` profile you give it; it never inherits another profile's Google account environment. Run a separate worker per profile.

## Google APIs, scopes and IAM

Enable the APIs in the OAuth client project:

- Gmail API (Gmail watches and history).
- Google Drive API (Drive changes and channels).
- Google Calendar API (Calendar events and channels).
- Cloud Pub/Sub API (Gmail push topic/subscription and pull).

Pub/Sub pull requires the `https://www.googleapis.com/auth/pubsub` or Cloud Platform scope plus subscription IAM; verify the granted scopes rather than assuming a login preset includes them. Either value of `has_attachment` requires `gmail.readonly`, `gmail.modify`, or `mail.google.com`, granted **without `gmail.metadata`**: Gmail rejects `format=full` when the metadata scope is also granted. Unknown or insufficient scope lists fail closed. Reauthorize with explicit custom scopes when needed.

`gws` 0.22.5 cannot dispatch generic Pub/Sub commands. The worker therefore implements only Pub/Sub pull and acknowledge via fixed Google HTTPS endpoints, using `gws auth export --unmasked` for the selected profile/account. Native gws decrypts; the worker keeps exported credentials and refreshed access tokens in memory only. It uses the existing grant, does not request wider scopes, rejects HTTP redirects, invalidates cached tokens after reauthorization, and never returns raw credential/HTTP errors. No second login or credential store is created. Encrypted native authorized-user accounts are required for this export path; plaintext-only/service-account imports are not supported by the Pub/Sub worker. OAuth export and each HTTP stage have bounded timeouts; credential output is buffered in memory and size-checked before parsing.

Gmail setup also needs:

- A Pub/Sub topic in the **same Google Cloud project as the OAuth client**.
- `gmail-api-push@system.gserviceaccount.com` granted **Pub/Sub Publisher** on that topic.
- A subscription the worker identity may **pull** and **acknowledge**.
- The `gws` account authorized with a scope that can call `users.watch`, `users.history.list`, and `users.stop`.

OAuth success alone does not prove Pub/Sub IAM. The plugin validates the topic's `projects/<id>/...` prefix against the project reported by `gws auth status` when available, but it does not provision IAM.

Drive and Calendar push notifications need a **public HTTPS callback** with a valid certificate. Terminate TLS at a reverse proxy or tunnel and forward **only** `/google/drive` and `/google/calendar` to the worker's loopback receiver (`127.0.0.1:8765` by default). Do not expose the worker's controls or the whole Hermes gateway. Google sends `X-Goog-Channel-Token`, an application-set bearer secret (not an HMAC signature); the worker validates it constant-time together with channel/resource identity from durable state. It never fetches a callback-supplied URL.

## Configuration

The same-session bridge requires `plugins.isolation: in_process` and
`plugins.entries.gws-oauth.allow_gateway_injection: true` in the target profile.
Hosted plugin hooks receive a placeholder session store, so same-session delivery
fails closed in `host` isolation; do not disable an isolation policy just to enable
this feature. Native `new_session` delivery does not require the live session store.
No setting is changed automatically.

Set these plugin settings in the target profile:

```bash
hermes config set plugins.entries.gws-oauth.settings.watch_gmail_topic \
  'projects/example-project/topics/gmail-watch'
hermes config set plugins.entries.gws-oauth.settings.watch_gmail_subscription \
  'projects/example-project/subscriptions/gmail-watch'
hermes config set plugins.entries.gws-oauth.settings.watch_callback_url \
  'https://watch.example.com'
```

`watch_callback_url` is the public base only; the worker appends `/google/drive` or `/google/calendar`. `watch_reconcile_seconds` defaults to 300 and is a cursor reconciliation interval, not full-resource polling.

Gmail start fails with `gmail_topic_missing`, `gmail_subscription_missing`, or `gmail_topic_project_mismatch` rather than creating a watch that cannot receive push. Drive/Calendar start fails with `drive_callback_missing`/`calendar_callback_missing` until the callback base is configured.

## Start the worker

Run it in the foreground first:

```bash
python /absolute/path/to/gws-oauth/watch_worker.py run \
  --home "$HERMES_HOME" \
  --gws-bin /absolute/path/to/gws \
  --gmail-topic 'projects/example-project/topics/gmail-watch' \
  --gmail-subscription 'projects/example-project/subscriptions/gmail-watch' \
  --callback-url 'https://watch.example.com' \
  --callback-host 127.0.0.1 \
  --callback-port 8765
```

The worker takes one advisory lock per profile (`plugin-data/gws-oauth/worker.lock`); a second worker for the same home refuses to start. The watch worker currently requires POSIX file locking; Windows watch-worker startup is unsupported. The included service example and verified lifecycle are Linux-only.

A Linux user-service example is in [`examples/gws-watch-worker.service`](../examples/gws-watch-worker.service). Use the same dependency-complete Python environment as Hermes (including `aiohttp` and `PyYAML`), not an arbitrary system Python. Replace every example path and keep the checkout/runtime immutable across restarts. The worker needs write access to the exact profile home: SQLite state, native account token refresh, and native webhook CAS/atomic replacement all write there. A writable webhook file alone is insufficient because atomic replacement creates a sibling temporary file. The example deliberately grants the whole selected profile write access; it is not isolation between accounts in that profile. No service is installed automatically.

Operator recovery:

```bash
python watch_worker.py list --home "$HERMES_HOME"
python watch_worker.py status --home "$HERMES_HOME" --stale-seconds 60
python watch_worker.py stop <watch_id> --home "$HERMES_HOME"
```

`stop` revokes local collection/forwarding first and reports remote cleanup separately. The worker performs owned route/channel cleanup on its next pass; operator-provisioned Pub/Sub topics and subscriptions are never deleted. A one-shot `new_session` route is retained after admission because Hermes exposes no supported completion observation. Explicit `stop` releases it after review and may cancel an admitted reply that has not yet been sent; already sent output cannot be recalled. Automatic expiry never removes a native route after a delivery attempt.

## Tool contract

### Retry, retention and diagnostics

Subscription create/renew and remote stop failures use durable exponential backoff (first retry after 10 seconds, capped at 900 seconds). Restarting the worker preserves the retry schedule; a fresh explicit start on an unused source clears it. Failed channel registrations retain one diagnostic error row rather than growing the database on every retry. Cursor reconciliation remains independent of push setup success.

Worker CLI `run --subscription-backoff-max SECONDS --retention-days DAYS` adjusts the defaults. These are worker arguments, not gateway plugin settings. Once an hour, fully cleaned terminal watches older than the retention window (default 7 days) are pruned along with obsolete hints and expired lease records. In-flight or uncertain delivery evidence, pending cleanup and retained native routes are not pruned. Age is measured from the watch expiry, so early stops may be retained longer.

`status` reports worker lock ownership, heartbeat age, watch counts, pending cleanup, retained routes, and source lease/error/retry summaries without secrets. It exits 1 when the worker is absent or its heartbeat is stale. A worker loop with slow upstream calls may exceed 60 seconds; choose a health threshold appropriate to the configured accounts and call timeouts. A healthy worker heartbeat is not proof of Google delivery or agent execution.

```json
{"tool":"gws_gmail_watch","arguments":{"action":"start","account":"user@example.com","target":{"thread_ids":["THREAD_A"]},"filters":{"from_addresses":["alice@example.com"]},"delivery_mode":"new_session","minutes":120,"stop_after_match":true}}
```

Common start fields: `account` (exact registered account), `target`, optional `filters`, `delivery_mode` (`same_session`, `new_session`), `minutes` 1–1440, `stop_after_match`, `batch_seconds` 0–60, bounded `instructions` (2000 chars), and `skills` only for `new_session`. Unknown and action-inappropriate fields are rejected. `status` optionally takes `watch_id`; `stop` requires it.

Service selectors:

- Gmail `target`: exactly one of `thread_ids`, `message_ids` (resolved to threads), or `mailbox:true`. Filters: `from_addresses`, `from_domains`, `label_ids`, `subject_contains`, `has_attachment`, `include_sent`. Sent/drafts are excluded unless enabled. Only headers/IDs/labels and, when needed, MIME filenames are read; no body, snippet, attachment bytes or raw mail.
- Drive `target`: exactly one of `file_ids`, `folder_id` (direct children only), or `all_files:true`. Optional `shared_drive_id`. Filters: `name_contains`, `mime_types`, `change_types`. Removed means deleted or no longer accessible; the plugin does not claim content diffs.
- Calendar `target`: `calendar_ids` required; optional `event_ids` or `recurring_event_ids`. Filters: `organizer_addresses`, `attendee_addresses`, `summary_contains`, `change_types` (`created|updated|cancelled`), `changed_fields`, `date_window`. `primary` is resolved to a concrete ID at start. Series + date window is rejected rather than misclassifying recurrence. Cancellations use prior metadata.

A reserved one-shot event survives its collection deadline until forwarding or explicit stop. A full delivery backlog pauses checkpoint advancement for the shared source instead of discarding matching events.

Rules: list values are OR, distinct filter fields are AND, string contains uses literal Unicode casefold (not regex), address predicates compare parsed addr-specs, domains compare exactly (no implicit subdomains), and IDs are preserved exactly. New watches get a catch-up barrier; existing files/messages/events are not announced as a historical flood. Matching happens before any model invocation.

## Honest limitations

- Same-session watches use the existing GOWA-style `pre_gateway_dispatch` bridge: check the stored physical session against the current session, then call `ctx.inject_message(session_key=...)`. No Hermes core patch is required. A reset observed before injection stops forwarding; a concurrent `/new` between this check and the host lookup can still race. Injection acceptance means scheduled, not delivered; ambiguous injections are not replayed.
- **Native completion is unobservable.** HTTP 202/duplicate proves admission only, not model execution or chat delivery. One-shot native routes are therefore retained; the watch reports `delivery_completion: "unobservable"`.
- **Push is not a guarantee.** The worker reconciles cursors independently of hints. Gmail history 404 triggers a bounded resync with an explicit unrecoverable-gap status; Calendar 410 rebuilds the snapshot and marks a coverage gap; Drive token rejection requires an explicit resync. Transient edits may coalesce upstream.
- Drive folder watches are direct children only. Calendar recurrence is master/exception metadata, never unbounded expansion. Gmail `users.watch` has no reliable watch-list endpoint; the plugin reports API acknowledgement and observed behavior.
- Attachment filename inspection covers four MIME levels; deeper attachments may be missed. Calendar attendee metadata is bounded to 100 entries. Calendar-wide date-window watches skip recurring masters; explicit series plus window is rejected.
- Metadata egress is bounded; matching may read more metadata locally than is forwarded. The plugin cannot detect every other application using the same Google topic/subscription; it stops only resources it owns.
- The included tests use a deterministic simulated Google transport, mocked delivery, and isolated real-host webhook CAS/injection API checks. They prove state-machine, selector, cursor, callback, and route-CAS mechanics; they are **not** live Google or native-session proof.

See [compatibility.md](compatibility.md) for the tested host/CLI baseline and CI scope. Re-validate the native webhook and approval seams after upgrading either.
