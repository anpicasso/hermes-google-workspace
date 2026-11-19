"""Strict agent-tool adapters for the three targeted Workspace watches."""
from __future__ import annotations

import json
import secrets

if __package__:
    from . import watch as W
    from . import watch_calendar, watch_drive, watch_gmail, watch_routes
    from .account_lifecycle import lifecycle_guard
else:  # Direct module use by the standalone test suite.
    import watch as W  # type: ignore
    import watch_calendar, watch_drive, watch_gmail, watch_routes  # type: ignore
    from account_lifecycle import lifecycle_guard

SERVICES = {
    "gmail": watch_gmail,
    "drive": watch_drive,
    "calendar": watch_calendar,
}

_START_FIELDS = {
    "gmail": {"action", "account", "target", "filters", "delivery_mode", "minutes",
              "stop_after_match", "batch_seconds", "instructions", "skills"},
    "drive": {"action", "account", "target", "filters", "delivery_mode", "minutes",
              "stop_after_match", "batch_seconds", "instructions", "skills", "shared_drive_id"},
    "calendar": {"action", "account", "target", "filters", "delivery_mode", "minutes",
                 "stop_after_match", "batch_seconds", "instructions", "skills"},
}


def _error(exc):
    if isinstance(exc, W.WatchError):
        return exc.as_dict()
    if isinstance(exc, watch_routes.RouteError):
        return {"ok": False, "error_code": exc.code, "error": exc.message}
    return {"ok": False, "error_code": "internal_error",
            "error": "The watch operation failed; inspect the worker status and local logs."}


def _same_route(watch, origin):
    bound = json.loads(watch["origin_json"])
    return all(bound.get(key) == origin.get(key) for key in
               ("profile", "principal", "session_key", "platform", "chat_id", "thread_id"))


def _owned(store, watch_id, service, origin):
    watch = store.get_watch(watch_id)
    if (watch is None or watch["service"] != service or watch["profile"] != origin["profile"]
            or watch["principal"] != origin["principal"] or not _same_route(watch, origin)):
        raise W.WatchError("not_found", "No accessible watch with that ID for this service")
    return watch


def handle(service, args, runtime, *, session_id=None):
    """Fence watch creation against companion logout/reauthorization.

    Lock order: lifecycle -> native execution locks -> short SQLite transaction.
    Status and stop do not acquire this barrier, so cleanup can still progress.
    """
    try:
        if isinstance(args, dict) and args.get("action") == "start":
            account = args.get("account")
            if not isinstance(account, str):
                raise W.WatchError("invalid_arguments", "start requires an account")
            with lifecycle_guard(runtime.store.home / "gws-oauth", W.validate_common(args)["account"]):
                return _handle(service, args, runtime, session_id=session_id)
        return _handle(service, args, runtime, session_id=session_id)
    except Exception as exc:
        return _error(exc)


def _handle(service, args, runtime, *, session_id=None):
    """Dispatch one tool call. ``runtime`` is built by the plugin registration."""
    module = SERVICES[service]
    if not isinstance(args, dict):
        return {"ok": False, "error_code": "invalid_arguments", "error": "arguments must be an object"}
    action = args.get("action")
    if action not in ("start", "status", "stop"):
        return {"ok": False, "error_code": "invalid_arguments", "error": "action must be start, status or stop"}
    allowed = _START_FIELDS[service] if action == "start" else {"action", "watch_id"}
    unknown = set(args) - allowed
    if unknown:
        return {"ok": False, "error_code": "invalid_arguments",
                "error": f"unknown or action-inappropriate fields: {', '.join(sorted(unknown))}"}
    try:
        origin = runtime.origin(session_id) if session_id is not None else runtime.origin()
        store = runtime.store
        store.expire_due()
        if action == "start":
            if "account" not in args or "target" not in args or "delivery_mode" not in args:
                raise W.WatchError("invalid_arguments",
                                   "start requires account, target and delivery_mode")
            common = W.validate_common(args)
            if common["mode"] == "same_session":
                capability = watch_routes.same_session_capability(runtime.ctx)
                if not capability["supported"]:
                    raise W.WatchError(capability["error_code"], capability["message"])
            prepared = module.prepare_start(runtime.client, common["account"], args,
                                            runtime.config)
            spec = {
                **common,
                "target": prepared["target"], "filters": prepared["filters"],
                "fingerprint": W.fingerprint(service, common["account"], origin, {
                    **common, "target": prepared["target"], "filters": prepared["filters"],
                }),
            }
            existing = store.find_active(spec["fingerprint"])
            if existing is not None:
                return {"ok": True, "action": "start", "already_active": True,
                        "watch": W.watch_status(store, existing)}
            watch_id = secrets.token_hex(8)
            stub = {"watch_id": watch_id, "service": service, "account": spec["account"],
                    "generation": 1}
            route = runtime.routes.create(stub, spec, origin)
            route_name = route["name"] if spec["mode"] == "new_session" else None
            bridge = route["name"] if spec["mode"] == "same_session" else None
            watch = None
            try:
                watch = W.create_watch(store, service, spec, origin, watch_id=watch_id,
                                       route_name=route_name, bridge_name=bridge,
                                       sources=prepared["sources"],
                                       baseline=prepared["baseline"])
                store.add_remote("route:" + watch_id, "native_route", watch_id=watch_id,
                                 identity={"name": route["name"], "mode": spec["mode"]},
                                 state="active")
            except Exception as exc:
                existing = store.get_watch(watch_id)
                if existing is not None:
                    store.stop(watch_id)
                    store.update_watch(watch_id, cleanup_state="pending",
                                       last_error="partial start compensation: " + type(exc).__name__)
                try:
                    runtime.routes.remove(route["name"], watch_id)
                except Exception:
                    pass
                raise
            return {"ok": True, "action": "start", "already_active": False,
                    "watch": W.watch_status(store, watch),
                    "resolved": prepared["resolved"]}
        if action == "stop":
            watch_id = args.get("watch_id")
            if not isinstance(watch_id, str) or not watch_id:
                raise W.WatchError("invalid_arguments", "stop requires an exact watch_id")
            watch = _owned(store, watch_id, service, origin)
            stopped = store.stop(watch_id)
            return {"ok": True, "action": "stop", "watch": W.watch_status(store, stopped),
                    "local_revoked": True,
                    "remote_cleanup": "pending",
                    "note": "Stop removes the local watch binding; operator routes are preserved. Already admitted or sent output cannot be undone."}
        watch_id = args.get("watch_id")
        if watch_id is not None:
            if not isinstance(watch_id, str) or not watch_id:
                raise W.WatchError("invalid_arguments", "watch_id must be an exact non-empty ID")
            watch = _owned(store, watch_id, service, origin)
            return {"ok": True, "action": "status", "watch": W.watch_status(store, watch)}
        rows = store.list_watches(service=service, profile=origin["profile"],
                                  principal=origin["principal"])
        rows = [row for row in rows if _same_route(row, origin)]
        return {"ok": True, "action": "status", "total": len(rows), "has_more": len(rows) > 50,
                "watches": [W.watch_status(store, row) for row in rows[:50]]}
    except Exception as exc:
        return _error(exc)


def bridge_callback(store, ctx, routes=None, *, profile="default"):
    return watch_routes.bridge_hook(store, ctx, routes, profile=profile)
