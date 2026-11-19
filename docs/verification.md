# Installed verification

Runtime tested: `5e7ffc29774b5d1ee664e3a8e4603cd5081cf124` (0.9.0), native gws 0.22.5, Linux. Later documentation-only commits do not change this runtime.

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

The public suite passed 207 tests; the private companion suite passed 69 tests. Native host validation/doctor and joint registration checks were exercised separately. CI also passed for the runtime commit above.

## What this does not prove

This was an operator-driven installed-code test, **not** a real user `/gws-bind` command or same-session gateway-delivery test. It did not exercise fresh browser consent, expired-account reauthorization, two simultaneously valid Google identities, Windows locking, or long-duration lease renewal. Do not infer those results from the successful three-service push test.

The documented same-session reset race and trusted-conversation boundary still apply; see [compatibility](compatibility.md). Existing-account API success does not imply that every stored account has valid consent.
