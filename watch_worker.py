"""One explicit, supervised GWS watch worker per Hermes profile.

Run it in the foreground or under a user service; never import-spawn it from
plugin registration. It owns Google subscriptions, durable deltas, callbacks,
renewal, cleanup and forwarding to native Hermes routes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import sys
import time
from collections import defaultdict
from contextlib import suppress
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - aiohttp is an existing Hermes dependency
    web = None

if __package__:
    from . import watch as W
    from . import watch_calendar, watch_drive, watch_gmail, watch_routes
    from .router import bounded_gws
    from .watch_pubsub import PubSubClient
else:  # Direct module use by the standalone test suite.
    from router import bounded_gws  # type: ignore
    from watch_pubsub import PubSubClient
    import watch as W  # type: ignore
    import watch_calendar, watch_drive, watch_gmail, watch_routes  # type: ignore

logger = logging.getLogger("plugins.gws_oauth.watch_worker")

TOOL = {"gmail": watch_gmail, "drive": watch_drive, "calendar": watch_calendar}
CHANNEL_KIND = {"drive": "drive_channel", "calendar": "calendar_channel"}
MAX_BODY = 1024
LEASE_SECONDS = {"gmail": 6 * 86400, "drive": 6 * 86400, "calendar": 6 * 86400}


class GwsClient:
    """Bounded native Workspace calls plus narrow account-bound Pub/Sub REST."""

    def __init__(self, home, *, gws_bin="gws", timeout=30):
        self.root = Path(home).expanduser().resolve() / "gws-oauth"
        self.gws_bin = gws_bin
        self.timeout = timeout
        self.pubsub = PubSubClient(self.root, gws_bin)

    def call(self, account, args, params=None, body=None, timeout=None):
        if list(args[:3]) == ["pubsub:v1", "projects", "subscriptions"]:
            if len(args) != 4 or set(params or {}) != {"subscription"}:
                return {"ok": False, "error_code": "invalid_arguments", "retryable": False}
            return self.pubsub.call(account, args[3], params["subscription"], body,
                                    timeout=timeout or self.timeout)
        argv = list(args)
        if params is not None:
            argv += ["--params", json.dumps(params, separators=(",", ":"), sort_keys=True)]
        if body is not None:
            argv += ["--json", json.dumps(body, separators=(",", ":"), sort_keys=True)]
        return bounded_gws(self.root, account, argv, gws_bin=self.gws_bin,
                           timeout=timeout or self.timeout, max_output=2_000_000)

    def scopes(self, account):
        result = bounded_gws(self.root, account, ["auth", "status"], gws_bin=self.gws_bin,
                             timeout=10, max_output=200_000)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return None
        scopes = result["data"].get("scopes")
        return [str(scope) for scope in scopes] if isinstance(scopes, list) else None

    def project(self, account):
        result = bounded_gws(self.root, account, ["auth", "status"], gws_bin=self.gws_bin,
                             timeout=10, max_output=200_000)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return None
        project = result["data"].get("project_id") or result["data"].get("project")
        return str(project) if isinstance(project, str) and project else None


class SingleInstance:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        if fcntl is None:
            # ponytail: no portable lock for this platform; refuse to pretend a
            # second worker is excluded, since the shared outbox would corrupt.
            raise RuntimeError("Single worker locking is unsupported on this platform")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self.handle = self.path.open("a+")
        try:
            if os.name != "nt":
                os.chmod(self.path, 0o600)
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another gws watch worker already owns this profile") from exc
        except Exception:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            if fcntl is not None:
                with suppress(OSError):
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def process_callback(store, headers, body=b""):
    """Pure callback validation + durable hint write. Returns (status, JSON)."""
    if len(body) > MAX_BODY:
        return 413, {"ok": False, "error": "body too large"}
    channel_id = str(headers.get("X-Goog-Channel-ID") or "").strip()
    token = str(headers.get("X-Goog-Channel-Token") or "").strip()
    resource_id = str(headers.get("X-Goog-Resource-ID") or "").strip()
    state = str(headers.get("X-Goog-Resource-State") or "").strip()
    if not channel_id or not token:
        return 400, {"ok": False, "error": "missing channel identity"}
    remote = store.get_remote("channel:" + channel_id)
    if remote is None:
        return 404, {"ok": False, "error": "unknown channel"}
    identity = json.loads(remote["identity_json"])
    if not hmac.compare_digest(str(identity.get("token") or ""), token):
        return 403, {"ok": False, "error": "bad channel token"}
    if resource_id and identity.get("resource_id") and resource_id != identity["resource_id"]:
        return 400, {"ok": False, "error": "resource mismatch"}
    message_number = str(headers.get("X-Goog-Message-Number") or "").strip()
    hint_id = json.dumps([channel_id, message_number, resource_id, state], separators=(",", ":"))
    try:
        store.add_hint(hint_id, remote["source_id"],
                       {"state": state, "resource_id": resource_id, "channel_id": channel_id})
    except Exception:
        return 503, {"ok": False, "error": "durable hint write failed"}
    return 200, {"ok": True, "sync": state == "sync"}


class CallbackServer:
    def __init__(self, worker, host="127.0.0.1", port=8765):
        self.worker = worker
        self.host = host
        self.port = port
        self.runner = None

    async def start(self):
        if web is None:
            raise RuntimeError("aiohttp is unavailable; Google callbacks cannot be received")
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_post("/google/drive", self._handle)
        app.router.add_post("/google/calendar", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def _handle(self, request):
        raw = await request.read()
        status, payload = await asyncio.to_thread(
            process_callback, self.worker.store, request.headers, raw)
        return web.json_response(payload, status=status)


class Worker:
    def __init__(self, home, client, routes, config, *, store=None, poster=None, clock=time.time):
        self.home = Path(home).expanduser().resolve()
        self.client = client
        self.routes = routes
        self.config = dict(config)
        self.store = store or W.WatchStore(self.home, clock=clock)
        self.poster = poster or routes.post
        self.clock = clock
        self.reconcile_seconds = int(config.get("watch_reconcile_seconds", 300))
        self.stopping = False

    # --- subscriptions ---------------------------------------------------
    def _channel(self, source, kind):
        channel_id = "gws-" + source["source_id"][:12] + "-" + secrets.token_hex(4)
        token = secrets.token_urlsafe(32)
        remote_id = "channel:" + channel_id
        self.store.add_remote(remote_id, kind, source_id=source["source_id"], state="pending",
                              identity={"token": token, "channel_id": channel_id,
                                        "resource_id": "", "source_id": source["source_id"]})
        self.store.update_source(source["source_id"], channel_id=channel_id,
                                 channel_token=token)
        return remote_id, channel_id, token

    def _address(self, service):
        base = str(self.config.get("watch_callback_url") or "").rstrip("/")
        if not base:
            return None
        return base + "/google/" + service

    def _ensure_gmail(self, source):
        topic = self.config.get("watch_gmail_topic")
        if not topic:
            return False
        remote = self.store.remote_for_source(source["source_id"], "gmail_watch")
        now = self.clock()
        if remote and (remote.get("expires_at") or 0) > now + 86400:
            return True
        result = self.client.call(source["account"], ["gmail", "users", "watch"], {"userId": "me"},
                                  body={"topicName": topic})
        if not result.get("ok"):
            self.store.update_source(source["source_id"], last_error=result.get("error", "Gmail watch failed"))
            return False
        data = result.get("data") or {}
        expiration = _expiry(data.get("expiration"))
        remote_id = "gmail:" + source["source_id"]
        self.store.add_remote(remote_id, "gmail_watch", source_id=source["source_id"],
                              identity={"historyId": str(data.get("historyId") or "")},
                              state="active", expires_at=expiration)
        self.store.update_source(source["source_id"], expires_at=expiration, last_error=None,
                                 **({"cursor": str(data["historyId"])}
                                    if not source.get("cursor") and str(data.get("historyId") or "").isdigit()
                                    else {}))
        return True

    def _ensure_channel(self, source, service):
        remote = self.store.remote_for_source(source["source_id"], CHANNEL_KIND[service])
        now = self.clock()
        if remote and (remote.get("expires_at") or 0) > now + 86400:
            return True
        address = self._address(service)
        if not address:
            self.store.update_source(source["source_id"], last_error="watch_callback_url is not configured")
            return False
        remote_id, channel_id, token = self._channel(source, CHANNEL_KIND[service])
        expiration = int((now + LEASE_SECONDS[service]) * 1000)
        body = {"id": channel_id, "type": "web_hook", "address": address,
                "token": token, "expiration": str(expiration)}
        params = {}
        if service == "drive":
            params = {"pageToken": source.get("cursor"), "includeItemsFromAllDrives": True,
                      "supportsAllDrives": True}
            drive_id = json.loads(source["resource_json"]).get("drive_id")
            if drive_id:
                params["driveId"] = drive_id
            result = self.client.call(source["account"], ["drive", "changes", "watch"], params, body=body)
        else:
            calendar_id = json.loads(source["resource_json"])["calendar_id"]
            params = {"calendarId": calendar_id}
            result = self.client.call(source["account"], ["calendar", "events", "watch"], params, body=body)
        if not result.get("ok"):
            self.store.add_remote(remote_id, CHANNEL_KIND[service], source_id=source["source_id"],
                                  state="error", identity={"token": token, "channel_id": channel_id},
                                  detail=result.get("error", "channel registration failed"))
            self.store.update_source(source["source_id"], last_error=result.get("error", "channel registration failed"))
            return False
        data = result.get("data") or {}
        resource_id = str(data.get("resourceId") or "")
        expires = _expiry(data.get("expiration")) or expiration / 1000.0
        self.store.update_remote(remote_id, state="active", expires_at=expires,
                                 identity_json=json.dumps({
                                     "token": token, "channel_id": channel_id,
                                     "resource_id": resource_id, "source_id": source["source_id"],
                                 }, sort_keys=True))
        self.store.update_source(source["source_id"], resource_id=resource_id, expires_at=expires,
                                 last_error=None)
        return True

    def ensure_subscription(self, source):
        service = source["service"]
        try:
            if service == "gmail":
                return self._ensure_gmail(source)
            if service in ("drive", "calendar"):
                return self._ensure_channel(source, service)
        except Exception:
            logger.warning("watch subscription failed for %s", service, exc_info=True)
        return False

    # --- delta reading ---------------------------------------------------
    def drain_source(self, source):
        watches = self.store.consumers(source["source_id"])
        if not watches:
            return {"ok": True, "skipped": "no consumers"}
        service = source["service"]
        try:
            if service == "gmail":
                pulled = watch_gmail.pubsub_pull(self.client, self.store, source, self.config)
                if not pulled.get("ok") and not pulled.get("error_code", "").startswith("gmail_"):
                    self.store.update_source(source["source_id"],
                                             last_error=pulled.get("error_code", "pubsub error"))
                result = watch_gmail.drain(self.client, self.store, source, watches)
            elif service == "drive":
                result = watch_drive.drain(self.client, self.store, source, watches)
            elif service == "calendar":
                result = watch_calendar.drain(self.client, self.store, source, watches)
            else:
                return {"ok": False, "error_code": "unknown_service"}
        except Exception:
            logger.warning("watch drain failed for %s", service, exc_info=True)
            self.store.update_source(source["source_id"], last_error="delta reader internal failure")
            return {"ok": False, "error_code": "internal_error", "retryable": True}
        if not result.get("ok"):
            detail = result.get("error_code", "delta error")
            self.store.update_source(source["source_id"], last_error=detail)
            if result.get("resync_required"):
                for watch in watches:
                    self.store.update_watch(
                        watch["watch_id"],
                        gap="Drive change token was rejected; source requires an explicit resync/restart")
            return result
        self.store.settle_hints(source["source_id"])
        self.store.update_source(source["source_id"], last_error=None)
        return result

    # --- forwarding ------------------------------------------------------
    def _backoff(self, attempts):
        return min(300, max(1, 2 ** min(int(attempts), 8))) + secrets.randbelow(5)

    def forward(self):
        due = self.store.due_outbox(self.clock())
        groups = defaultdict(list)
        for row in due:
            groups[row["watch_id"]].append(row)
        for watch_id, rows in groups.items():
            watch = self.store.get_watch(watch_id)
            if watch is None:
                for row in rows:
                    self.store.set_event(row["event_id"], "cancelled", detail="watch no longer exists")
                continue
            if watch["state"] not in W.ACTIVE_STATES:
                for row in rows:
                    self.store.set_event(row["event_id"], "cancelled", detail="collection stopped")
                continue
            if watch["mode"] == "same_session":
                for row in rows:
                    self._forward_bridge(watch, row)
                continue
            self._forward_native(watch, rows)

    def _forward_native(self, watch, rows):
        # Rows that already carry a request id are an immutable accepted batch.
        # Never merge newly due events into them: Hermes dedupes by request id,
        # so a changed payload would be silently discarded as a duplicate.
        assigned = defaultdict(list)
        fresh = []
        for row in rows:
            if row.get("request_id"):
                assigned[row["request_id"]].append(row)
            else:
                fresh.append(row)
        for request_id, batch in assigned.items():
            self._send_native_batch(watch, batch, request_id)
        if not fresh:
            return
        oldest = min(row["created_at"] for row in fresh)
        if (watch["batch_seconds"] and not watch["stop_after_match"]
                and len(fresh) == 1 and self.clock() - oldest < watch["batch_seconds"]):
            return
        request_id = "gws-" + hashlib.sha256(
            ",".join(sorted(row["event_id"] for row in fresh)).encode()).hexdigest()[:40]
        self._send_native_batch(watch, fresh, request_id)

    def _send_native_batch(self, watch, rows, request_id):
        watch_now, claimed = self.store.claim_outbox(
            [row["event_id"] for row in rows], state="forwarding",
            expected_states=("pending", "retry"), request_id=request_id,
            generation=int(watch["generation"]))
        if watch_now is None or not claimed:
            return
        event_ids = [row["event_id"] for row in claimed]
        events = [json.loads(row["payload_json"]) for row in claimed]
        payload = {"event_type": "gws-watch", "watch_id": watch_now["watch_id"],
                   "service": watch_now["service"], "account": watch_now["account"],
                   "generation": watch_now["generation"], "events": events}
        try:
            result = self.poster(watch_now["route_name"], payload, request_id)
        except Exception:
            result = {"ok": False, "error_code": "native_route_internal", "retryable": True}
        if result.get("ok"):
            detail = "native admission accepted; completion and external delivery are unobservable"
            if self.store.finish_outbox(event_ids, "admitted",
                                        expected_states=("forwarding",), detail=detail):
                self.store.update_watch(watch_now["watch_id"], delivery_state="admitted",
                                        last_error=None)
            return
        if result.get("retryable"):
            changed = 0
            for row in claimed:
                changed += self.store.finish_outbox(
                    [row["event_id"]], "retry", expected_states=("forwarding",),
                    detail=result.get("error_code", "retry"),
                    next_attempt=self.clock() + self._backoff(row["attempts"]))
            if changed:
                self.store.update_watch(watch_now["watch_id"], delivery_state="pending",
                                        last_error=result.get("error_code", "native route retry"))
            return
        if self.store.finish_outbox(event_ids, "failed",
                                    expected_states=("forwarding",),
                                    detail=result.get("error_code", "rejected")):
            self.store.update_watch(watch_now["watch_id"], delivery_state="failed",
                                    last_error=result.get("error_code", "native route rejected"))

    def _forward_bridge(self, watch, row):
        if not watch.get("bridge_name"):
            if self.store.finish_outbox([row["event_id"]], "failed",
                                        expected_states=("pending", "retry"),
                                        detail="bridge route missing"):
                self.store.update_watch(watch["watch_id"], delivery_state="failed",
                                        last_error="bridge route missing")
            return
        request_id = row.get("request_id") or ("gws-" + hashlib.sha256(
            row["event_id"].encode()).hexdigest()[:40])
        watch_now, claimed = self.store.claim_outbox(
            [row["event_id"]], state="forwarding",
            expected_states=("pending", "retry"), request_id=request_id,
            generation=int(watch["generation"]))
        if watch_now is None or not claimed:
            return
        row = claimed[0]
        payload = {"event_type": "gws-watch-bridge", "watch_id": watch_now["watch_id"],
                   "event_id": row["event_id"], "generation": watch_now["generation"]}
        try:
            result = self.poster(watch_now["bridge_name"], payload, request_id)
        except Exception:
            result = {"ok": False, "error_code": "bridge_internal", "retryable": True}
        if result.get("ok"):
            # The hook may already have scheduled the event; only a still-
            # forwarding row gets this informational acknowledgement.
            self.store.bridge_accepted(
                row["event_id"],
                "bridge request accepted by Hermes; awaiting same-session admission")
            return
        if result.get("retryable"):
            if self.store.finish_outbox(
                    [row["event_id"]], "retry", expected_states=("forwarding",),
                    detail=result.get("error_code", "retry"),
                    next_attempt=self.clock() + self._backoff(row["attempts"])):
                self.store.update_watch(watch_now["watch_id"], delivery_state="pending",
                                        last_error=result.get("error_code", "bridge retry"))
            return
        if self.store.finish_outbox([row["event_id"]], "failed",
                                    expected_states=("forwarding",),
                                    detail=result.get("error_code", "rejected")):
            self.store.update_watch(watch_now["watch_id"], delivery_state="failed",
                                    last_error=result.get("error_code", "bridge rejected"))

    # --- cleanup ---------------------------------------------------------
    def _stop_source(self, source):
        remotes = self.store.remotes(source_id=source["source_id"])
        failed = False
        for remote in remotes:
            if remote["kind"] == "gmail_watch":
                result = self.client.call(source["account"], ["gmail", "users", "stop"], {"userId": "me"})
            elif remote["kind"] in ("drive_channel", "calendar_channel"):
                identity = json.loads(remote["identity_json"])
                params = {"id": identity.get("channel_id"), "resourceId": identity.get("resource_id")}
                if not params["id"] or not params["resourceId"]:
                    continue
                result = self.client.call(source["account"], ["drive", "channels", "stop"]
                                          if remote["kind"] == "drive_channel"
                                          else ["calendar", "channels", "stop"], {}, body=params)
            else:
                continue
            if result.get("ok") or result.get("http_status") in (404, 410):
                self.store.remove_remote(remote["remote_id"])
            else:
                failed = True
                self.store.update_remote(remote["remote_id"], state="cleanup_pending",
                                         detail=result.get("error_code", "stop failed"))
        if not failed:
            self.store.update_source(source["source_id"], state="stopped")
        return not failed

    def cleanup(self):
        for watch in self.store.list_watches(service=None):
            if watch["state"] not in ("stopped", "expired", "matched"):
                continue
            if watch["cleanup_state"] == "done":
                continue
            sources_ok = True
            for source in self.store.sources_for(watch["watch_id"]):
                if self.store.active_consumers_excluding(source["source_id"], watch["watch_id"]) == 0:
                    sources_ok = self._stop_source(source) and sources_ok
            delivered = self.store.delivery_summary(watch["watch_id"])
            route_name = watch["route_name"] or watch["bridge_name"]
            # Keep reserved notifications routable until their dispatch settles.
            waiting = watch["state"] == "matched" and delivered["backlog"] > 0
            native_attempted = any(row["attempts"] or row["state"] in ("admitted", "uncertain")
                                   for row in self.store.outbox(watch["watch_id"]))
            retain = bool(watch["route_name"] and watch["state"] != "stopped"
                          and native_attempted)
            if waiting:
                state, reason = "pending", "one-shot notification still in flight"
            elif retain:
                state = "retained" if sources_ok else "pending"
                reason = "native completion is unobservable; route retained until explicit stop"
            else:
                removed = (self.routes.remove(route_name, watch["watch_id"]) if route_name
                           else {"removed": True, "state": "absent"})
                state = "done" if removed.get("removed") and sources_ok else "pending"
                reason = f"route={removed.get('state')}; sources={'ok' if sources_ok else 'pending'}"
            self.store.update_watch(watch["watch_id"], cleanup_state=state, cleanup_reason=reason)

    # --- loop ------------------------------------------------------------
    def run_once(self):
        self.store.expire_due()
        self.store.recover_stale_outbox(self.clock())
        for source in self.store.list_sources(active_only=True):
            watches = self.store.consumers(source["source_id"])
            if not watches:
                self._stop_source(source)
                continue
            if not self.ensure_subscription(source):
                continue
            pending = self.store.pending_hints(source["source_id"])
            reconcile_due = ((source.get("last_reconcile") or 0) + self.reconcile_seconds
                             <= self.clock())
            if source["service"] == "gmail" or pending or reconcile_due:
                self.drain_source(source)
        self.cleanup()
        self.forward()
        self.store.set_meta("worker_heartbeat", self.clock())
        return {"ok": True}

    async def run_forever(self, *, interval=5, callback_host="127.0.0.1", callback_port=8765):
        server = None
        if web is not None and callback_port:
            server = CallbackServer(self, callback_host, callback_port)
            await server.start()
        try:
            while not self.stopping:
                await asyncio.to_thread(self.run_once)
                await asyncio.sleep(max(1, interval))
        finally:
            if server is not None:
                await server.stop()


def _expiry(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / 1000.0 if number > 10_000_000_000 else number


def _lock_path(home):
    return Path(home).expanduser().resolve() / "plugin-data" / "gws-oauth" / "worker.lock"


def _store(home):
    return W.WatchStore(home)


def _print(value):
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="watch_worker", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the single profile worker in the foreground")
    run.add_argument("--home", required=True, help="Exact Hermes profile home")
    run.add_argument("--gws-bin", default="gws")
    run.add_argument("--callback-url", default="")
    run.add_argument("--gmail-topic", default="")
    run.add_argument("--gmail-subscription", default="")
    run.add_argument("--callback-host", default="127.0.0.1")
    run.add_argument("--callback-port", type=int, default=8765)
    run.add_argument("--interval", type=int, default=5)
    run.add_argument("--reconcile-seconds", type=int, default=300)
    listing = sub.add_parser("list", help="list this profile's watches")
    listing.add_argument("--home", required=True)
    stop = sub.add_parser("stop", help="revoke one watch locally; the worker finishes remote cleanup")
    stop.add_argument("watch_id")
    stop.add_argument("--home", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "list":
        store = _store(args.home)
        _print([W.watch_status(store, row) for row in store.list_watches()])
        return 0
    if args.command == "stop":
        store = _store(args.home)
        row = W.operator_stop(store, args.watch_id)
        _print({"ok": True, "watch": W.watch_status(store, row), "remote_cleanup": "pending"})
        return 0
    config = {"watch_callback_url": args.callback_url,
              "watch_gmail_topic": args.gmail_topic,
              "watch_gmail_subscription": args.gmail_subscription,
              "watch_reconcile_seconds": args.reconcile_seconds}
    os.environ["HERMES_HOME"] = str(Path(args.home).expanduser().resolve())
    client = GwsClient(args.home, gws_bin=args.gws_bin)
    routes = watch_routes.RouteManager(home=args.home)
    worker = Worker(args.home, client, routes, config)
    with SingleInstance(_lock_path(args.home)):
        try:
            asyncio.run(worker.run_forever(interval=args.interval,
                                           callback_host=args.callback_host,
                                           callback_port=args.callback_port))
        except KeyboardInterrupt:
            worker.stopping = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
