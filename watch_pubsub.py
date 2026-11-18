"""Narrow Pub/Sub pull/ack transport; gws 0.22.5 cannot dispatch this API.

Native gws decrypts the selected account. Exported credentials and refreshed
access tokens stay in worker memory, never in results, logs, or spill files.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if __package__:
    from .router import _account_env, _native_guard, account_dir
else:
    from router import _account_env, _native_guard, account_dir

_SUBSCRIPTION = re.compile(r"projects/[A-Za-z0-9][A-Za-z0-9._~-]{0,127}/subscriptions/[A-Za-z][A-Za-z0-9._~+-]{2,254}\Z")
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, data, headers, timeout, limit=2_000_000):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("response too large")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("invalid response")
    return result


class PubSubClient:
    def __init__(self, root, gws_bin="gws"):
        self.root = Path(root).resolve()
        self.gws_bin = gws_bin
        self._tokens = {}

    def call(self, account, operation, subscription, body, timeout=30):
        if (operation not in ("pull", "acknowledge")
                or not isinstance(subscription, str) or not _SUBSCRIPTION.fullmatch(subscription)
                or not isinstance(body, dict)):
            return {"ok": False, "error_code": "invalid_arguments", "retryable": False}
        if operation == "pull":
            valid = (set(body) <= {"maxMessages", "returnImmediately"}
                     and type(body.get("maxMessages")) is int and 1 <= body["maxMessages"] <= 100
                     and type(body.get("returnImmediately", False)) is bool)
        else:
            ids = body.get("ackIds")
            valid = (set(body) == {"ackIds"} and isinstance(ids, list) and 1 <= len(ids) <= 100
                     and all(isinstance(item, str) and 0 < len(item) <= 4096 for item in ids))
        if not valid:
            return {"ok": False, "error_code": "invalid_arguments", "retryable": False}
        stage = "credentials"
        try:
            with _native_guard(self.root, account):
                path = account_dir(self.root, account)
                # Export every call so reauthorization invalidates the in-memory
                # token immediately; no second credential store or login flow.
                exported = subprocess.run(
                    [self.gws_bin, "auth", "export", "--unmasked"],
                    env=_account_env(path), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=timeout, check=False,
                )
                if exported.returncode or len(exported.stdout) > 65536:
                    raise ValueError("export failed")
                credentials = json.loads(exported.stdout)
                keys = ("client_id", "client_secret", "refresh_token")
                if (not isinstance(credentials, dict)
                        or credentials.get("type") != "authorized_user"
                        or not all(isinstance(credentials.get(k), str)
                                   and 0 < len(credentials[k]) <= 16384 for k in keys)):
                    raise ValueError("unsupported credentials")
                fingerprint = hashlib.sha256(json.dumps(
                    [credentials[k] for k in keys], separators=(",", ":")).encode()).digest()
                cached = self._tokens.get(account)
                if not cached or cached[0] != fingerprint or cached[2] <= time.monotonic():
                    self._tokens.pop(account, None)
                    stage = "token"
                    form = {k: credentials[k] for k in keys}
                    form["grant_type"] = "refresh_token"
                    token = _post(_TOKEN_URL, urllib.parse.urlencode(form).encode(),
                                  {"Content-Type": "application/x-www-form-urlencoded"}, timeout, 65536)
                    access = token.get("access_token")
                    ttl = token.get("expires_in")
                    if (not isinstance(access, str) or not access or len(access) > 16384
                            or any(ord(c) < 33 or ord(c) > 126 for c in access)
                            or type(ttl) is not int or not 0 < ttl <= 86400):
                        raise ValueError("invalid token response")
                    cached = (fingerprint, access, time.monotonic() + max(0, ttl - 60))
                    self._tokens[account] = cached
                stage = "pubsub"
                data = _post(f"https://pubsub.googleapis.com/v1/{subscription}:{operation}",
                             json.dumps(body, separators=(",", ":")).encode(),
                             {"Content-Type": "application/json", "Authorization": "Bearer " + cached[1]},
                             timeout)
                return {"ok": True, "data": data}
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401 or stage == "token":
                self._tokens.pop(account, None)
            return {"ok": False, "error_code": "pubsub_" + stage,
                    "http_status": status, "retryable": status == 429 or status >= 500,
                    "error": "Google rejected the request; check account authorization, Pub/Sub scope and IAM."}
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            return {"ok": False, "error_code": "pubsub_" + stage,
                    "retryable": stage != "credentials",
                    "error": "Pub/Sub transport failed; verify native gws encrypted account export and connectivity."}
