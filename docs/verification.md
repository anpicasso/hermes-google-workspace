# Installed verification

Runtime tested: `5e7ffc29774b5d1ee664e3a8e4603cd5081cf124` (0.9.0), native gws 0.22.5, Linux. The installed check below predates the shared native account backend.

## Real Google PUSH → Hermes → Discord

An explicitly authorized operator harness exercised the installed plugin with an existing Google account, a supervised worker, public HTTPS callback forwarding and preprovisioned native Hermes webhook routes:

- Gmail: sent a uniquely tagged message to the same account.
- Drive: renamed a disposable file and read the change back.
- Calendar: changed a disposable event and read the change back.
- Each service received authenticated/validated callback hints and reconciled through native gws.
- All three watch outboxes were admitted on their first attempt; all three native admissions completed.
- All three native delivery obligations reached `delivered`, with no delivery error. Retained destinations were checked against the intended Discord thread; HTTP acceptance alone was not counted as delivery.
- All watches were stopped and reached cleanup state `done`. The test message and file were trashed, and the event was deleted; cleanup was read back.

The worker remains supervised; installation itself does not provision this infrastructure. See [watch setup](watch-setup.md).

## Automated checks

The historical installed runtime passed its own suites and CI. For the shared account backend, the public suite drives `hermes gws` with isolated native subprocesses. The matching private suite loads both actual plugins through the pinned Hermes loader and exercises configured off-PATH login, callback/session routing, cancellation, identity validation, watch refusal, local logout and CLI epoch fencing. These use synthetic identities and local loopback only, not Google or a platform adapter. See [compatibility](compatibility.md) for the reproducible host baseline.

## What this does not prove

This was an operator-driven installed-code test, **not** a real user `/gws-bind` command or same-session gateway-delivery test. It did not exercise fresh browser consent, expired-account reauthorization, two simultaneously valid Google identities, Windows locking, or long-duration lease renewal. Do not infer those results from the successful three-service push test.

The documented same-session reset race and trusted-conversation boundary still apply; see [compatibility](compatibility.md). Existing-account API success does not imply that every stored account has valid consent.
