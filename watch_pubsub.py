"""Authenticated, operator-preprovisioned Pub/Sub PUSH; no OAuth credentials.

Only Google's public signing certificates are fetched. The operator owns topic,
subscription, IAM, OIDC audience/service account and HTTPS proxy provisioning.
"""
from __future__ import annotations

import base64
import json
import re
from functools import partial
from urllib.parse import urlsplit

if __package__:
    from . import watch as W
else:
    import watch as W

MAX_BODY = 16 * 1024
_RESOURCE = re.compile(r"projects/[A-Za-z0-9][A-Za-z0-9._~-]{0,127}/(topics|subscriptions)/[A-Za-z][A-Za-z0-9._~+-]{0,254}\Z")


def validate_config(config):
    for key, kind, code in (("watch_gmail_topic", "topics", "gmail_topic"),
                            ("watch_gmail_subscription", "subscriptions", "gmail_subscription")):
        value = config.get(key)
        if not value:
            raise W.WatchError(code + "_missing", "Configure " + key + " before starting Gmail PUSH")
        if not isinstance(value, str) or not _RESOURCE.fullmatch(value) or value.split('/')[2] != kind:
            raise W.WatchError(code + ("_invalid" if kind == "topics" else "_missing"),
                               key + " must be an exact projects/<project>/" + kind + "/<name>")
    audience = config.get("watch_gmail_push_audience")
    account = config.get("watch_gmail_push_service_account")
    callback = config.get("watch_callback_url")
    if (not isinstance(audience, str) or not audience or audience != audience.strip()
            or len(audience) > 2048 or any(ord(c) < 33 for c in audience)):
        raise W.WatchError("gmail_push_config", "Set watch_gmail_push_audience to the exact OIDC audience")
    if (not isinstance(account, str) or not re.fullmatch(
            r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com", account)):
        raise W.WatchError("gmail_push_config", "Set watch_gmail_push_service_account to the exact service account email")
    try:
        url = urlsplit(callback if isinstance(callback, str) else "")
        valid = (url.scheme == "https" and url.hostname and not url.username and not url.password
                 and not url.query and not url.fragment and callback == callback.strip()
                 and not any(ord(c) <= 32 for c in callback))
        url.port
    except ValueError:
        valid = False
    if not valid:
        raise W.WatchError("gmail_push_config", "Set watch_callback_url to a public HTTPS base URL")
    try:
        from google.oauth2.id_token import verify_oauth2_token  # noqa: F401
        from google.auth.transport.requests import Request  # noqa: F401
    except ImportError as exc:
        raise W.WatchError("gmail_push_dependency", "Gmail PUSH requires google-auth and requests") from exc
    return audience, account


def verify_identity(token, audience, service_account):
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import verify_oauth2_token
    import requests

    # Official verifier checks signature, expiry, issued-at, audience and issuer.
    # No ADC, account credential, OAuth export or token creation is involved.
    with requests.Session() as session:
        claims = verify_oauth2_token(token, partial(Request(session=session), timeout=10),
                                     audience=audience)
    return (claims.get("iss") in ("accounts.google.com", "https://accounts.google.com")
            and claims.get("aud") == audience and claims.get("email") == service_account
            and claims.get("email_verified") is True)


def process_push(store, config, headers, body):
    """Return HTTP status only after a validated hint is durable; replay is safe."""
    if len(body) > MAX_BODY:
        return 413, {"ok": False, "error": "body too large"}
    try:
        audience, service_account = validate_config(config)
    except W.WatchError as exc:
        return 503, exc.as_dict()
    authorization = headers.get("Authorization", "")
    if (not isinstance(authorization, str) or not authorization.startswith("Bearer ")
            or not 0 < len(authorization[7:]) <= 8192):
        return 401, {"ok": False, "error": "missing OIDC bearer"}
    try:
        if not verify_identity(authorization[7:], audience, service_account):
            raise ValueError("identity mismatch")
    except Exception:
        # Never reflect JWTs or verifier error details into responses/logs.
        return 403, {"ok": False, "error": "OIDC verification failed"}
    try:
        envelope = json.loads(body)
        if not isinstance(envelope, dict) or envelope.get("subscription") != config["watch_gmail_subscription"]:
            raise ValueError("subscription mismatch")
        message = envelope["message"]
        message_id = message["messageId"]
        if not isinstance(message_id, str) or not 0 < len(message_id) <= 256:
            raise ValueError("message identity missing")
        payload = json.loads(base64.b64decode(message["data"], validate=True))
        address = payload["emailAddress"]
        if W.clean_address(address) != address:
            raise ValueError("noncanonical mailbox")
        history = payload["historyId"]
        if type(history) is int and history >= 0:
            history = str(history)
        if not isinstance(history, str) or not re.fullmatch(r"[0-9]{1,40}", history):
            raise ValueError("invalid history")
    except (ValueError, TypeError, KeyError, W.WatchError):
        return 400, {"ok": False, "error": "invalid Pub/Sub envelope"}
    try:
        # One mailbox source per exact registered account, never aliases or a
        # fallback to whichever account happens to be running this iteration.
        source_id = store.source_key("gmail", address, {"mailbox": "me"}, {})
        source = store.get_source(source_id)
        if not source or source["state"] != "active" or not store.consumers(source_id):
            return 404, {"ok": False, "error": "mailbox is not watched"}
        store.add_hint("gmail:" + source_id + ":" + history, source_id, {"history_id": history})
    except Exception:
        return 503, {"ok": False, "error": "durable hint write failed"}
    return 204, {"ok": True}
