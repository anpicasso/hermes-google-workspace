import asyncio
import copy
import hashlib
import hmac
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_routes
from watch_helpers import Clock, make_origin


def config_for(origin):
    return {"routes": [{"origin": {key: origin.get(key) for key in watch_routes._ORIGIN_FIELDS},
        "new_session": {"name": "gws-native", "url": "http://127.0.0.1:8644/webhooks/gws-native",
                        "secret": "synthetic-secret-" * 3,
                        "destination": {key: origin.get(key) for key in ("platform", "chat_id", "thread_id")}},
        "same_session": {"bridge_name": "gws-watch-bridge-main", "url": "http://127.0.0.1:8644/webhooks/gws-watch-bridge-main",
                         "secret": "synthetic-bridge-secret-" * 3}}]}


class DummyResponse:
    def __init__(self, payload, status=202):
        self.payload, self.status = payload, status

    def read(self, limit):
        assert limit == 65536
        return json.dumps(self.payload).encode()[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = watch.WatchStore(self.tmp.name, clock=self.clock)
        self.origin = make_origin()
        self.config = config_for(self.origin)
        self.injected = []
        self.ctx = SimpleNamespace(inject_message=lambda content, *, session_key=None:
                                   self.injected.append((content, session_key)) or True)
        self.manager = watch_routes.RouteManager(self.ctx, store=self.store, config=lambda: self.config)
        self.stub = {"watch_id": "a" * 16, "service": "gmail", "account": "person@example.com", "generation": 1}
        self.spec = {"mode": "new_session", "instructions": "Summarize it", "skills": []}
        self.sessions = SimpleNamespace(lookup_by_session_key=lambda key: SimpleNamespace(session_id="session-1"))

    def create(self, mode="new_session"):
        self.spec["mode"] = mode
        return self.manager.create(self.stub, self.spec, self.origin)

    def payload(self):
        return {"event_type": "gws-watch", "watch_id": self.stub["watch_id"], "generation": 1, "events": []}

    def bridge(self):
        binding = self.create("same_session")
        spec = watch.validate_common({"account": "person@example.com", "delivery_mode": "same_session",
                                      "minutes": 60})
        spec.update(target={"mailbox": True}, filters={}, fingerprint="fixture")
        row = watch.create_watch(self.store, "gmail", spec, self.origin, watch_id=self.stub["watch_id"],
                                 bridge_name=binding["name"], sources=[], baseline={})
        self.store.add_event(row["watch_id"], "event-1", "gmail", {"x": 1})
        event = SimpleNamespace(raw_message={"event_type": "gws-watch-bridge", "watch_id": row["watch_id"],
                                            "event_id": "event-1", "generation": 1},
                                source=SimpleNamespace(chat_name="webhook/gws-watch-bridge-main", platform="webhook",
                                                       user_id="webhook:gws-watch-bridge-main", profile="default"))
        return row, event, watch_routes.bridge_hook(self.store, self.ctx, self.manager)

    def state(self):
        return self.store.outbox(self.stub["watch_id"])[0]["state"]

    def test_same_session_capability_requires_callable_injection(self):
        self.assertFalse(watch_routes.same_session_capability(SimpleNamespace())["supported"])
        self.assertFalse(watch_routes.same_session_capability(SimpleNamespace(inject_message=None))["supported"])
        self.ctx._gateway_injection_allowed = lambda: self.fail("private method must not be called")
        self.assertTrue(watch_routes.same_session_capability(self.ctx)["supported"])

    def test_new_session_route_is_immutable_operator_contract(self):
        before = copy.deepcopy(self.config)
        binding = self.create()
        self.assertEqual(self.config, before)
        self.assertEqual(self.manager.read(binding["name"]), binding)
        self.assertNotIn("synthetic-secret", json.dumps(binding))
        with self.store.tx() as conn:
            self.assertNotIn("synthetic-secret", conn.execute("SELECT metadata FROM route_bindings").fetchone()[0])
        restarted = watch_routes.RouteManager(store=self.store, config=self.config)
        self.assertEqual(restarted.read(binding["name"]), binding)
        self.assertEqual(self.create(), binding)

    def test_same_session_route_is_bridge_without_event_prompt(self):
        before = copy.deepcopy(self.config)
        binding = self.create("same_session")
        self.assertEqual(binding["bridge_name"], "gws-watch-bridge-main")
        self.assertEqual(self.config, before)
        self.assertNotIn("prompt", self.config["routes"][0]["same_session"])

    def test_static_and_foreign_routes_are_never_shadowed(self):
        path = Path(self.tmp.name) / "webhook_subscriptions.json"
        path.write_text('{"foreign": true}')
        binding = self.create()
        self.assertFalse(self.manager.remove(binding["name"], "foreign")["removed"])
        self.assertEqual(path.read_text(), '{"foreign": true}')
        self.stub["generation"] = 2
        with self.assertRaises(watch_routes.RouteError):
            self.create()

    def test_corrupt_host_store_is_never_read_or_mutated(self):
        path = Path(self.tmp.name) / "webhook_subscriptions.json"
        path.write_text("{not-json")
        binding = self.create()
        self.manager.remove(binding["name"], self.stub["watch_id"])
        self.assertEqual(path.read_text(), "{not-json")

    def test_operator_disabled_and_edited_routes_are_preserved(self):
        for field, value in (("enabled", False), ("description", "operator edit")):
            with self.subTest(field=field):
                binding = self.create()
                self.config["routes"][0]["new_session"][field] = value
                before = copy.deepcopy(self.config)
                self.assertTrue(self.manager.remove(binding["name"], self.stub["watch_id"])["removed"])
                self.assertEqual(self.config, before)
                self.config = config_for(self.origin)

    def test_operator_disabled_route_cannot_be_enabled(self):
        self.config["routes"][0]["new_session"]["enabled"] = False
        with self.assertRaises(watch_routes.RouteError):
            self.create()
        self.assertIs(self.config["routes"][0]["new_session"]["enabled"], False)

    def test_origin_destination_profile_and_ambiguity_fail_closed(self):
        for mutation in (lambda: self.origin.update(session_id=""),
                         lambda: self.origin.update(profile="other"),
                         lambda: self.config["routes"][0]["new_session"]["destination"].update(chat_id="foreign"),
                         lambda: self.config["routes"].append(copy.deepcopy(self.config["routes"][0]))):
            with self.subTest(mutation=mutation):
                self.origin = make_origin()
                self.config = config_for(self.origin)
                mutation()
                with self.assertRaises(watch_routes.RouteError):
                    self.create()

    def test_hmac_v2_stable_request_id_and_bounded_response(self):
        binding = self.create()
        with mock.patch("watch_routes.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = DummyResponse({"status": "accepted"})
            for _ in range(2):
                self.assertTrue(self.manager.post(binding["name"], self.payload(), "request-1")["ok"])
            requests = [call.args[0] for call in opener.return_value.open.call_args_list]
        for request in requests:
            timestamp = request.get_header("X-webhook-timestamp")
            expected = hmac.new(self.manager.secret(binding["name"]).encode(),
                                timestamp.encode() + b"." + request.data, hashlib.sha256).hexdigest()
            self.assertEqual(request.get_header("X-webhook-signature-v2"), expected)
            self.assertEqual(request.get_header("X-request-id"), "request-1")
        self.assertEqual(requests[0].data, requests[1].data)

    def test_post_requires_admitted_receipt(self):
        binding = self.create()
        with mock.patch("watch_routes.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = DummyResponse({"status": "ignored"})
            self.assertTrue(self.manager.post(binding["name"], self.payload(), "r")["retryable"])
            opener.return_value.open.return_value = DummyResponse({"status": "duplicate"}, 200)
            self.assertTrue(self.manager.post(binding["name"], self.payload(), "r")["duplicate"])

    def test_config_change_and_disabled_refuse_without_sending(self):
        binding = self.create()
        for key, value in (("url", "https://other.invalid/webhooks/gws-native"),
                           ("secret", "changed-secret" * 4), ("enabled", False)):
            self.config = config_for(self.origin)
            self.config["routes"][0]["new_session"][key] = value
            with mock.patch("watch_routes.urllib.request.build_opener") as opener:
                result = self.manager.post(binding["name"], self.payload(), "r")
                self.assertFalse(result["ok"])
                self.assertFalse(result["retryable"])
                opener.assert_not_called()

    def test_redirect_is_not_followed_real_loopback(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/stolen")
                self.end_headers()
            def log_message(self, format, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.config["routes"][0]["new_session"]["url"] = f"http://127.0.0.1:{server.server_port}/webhooks/gws-native"
            binding = self.create()
            result = self.manager.post(binding["name"], self.payload(), "stable")
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], 307)
            self.assertEqual(hits, ["/webhooks/gws-native"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_start_same_session_fails_closed_without_injection(self):
        self.manager.ctx = SimpleNamespace()
        with self.assertRaises(watch_routes.RouteError):
            self.create("same_session")

    def test_bridge_consumes_owned_event_and_replay_only_once(self):
        row, event, hook = self.bridge()
        async def simultaneous():
            return await asyncio.gather(*(hook(event, session_store=self.sessions) for _ in range(4)))
        self.assertTrue(all(r["action"] == "skip" for r in asyncio.run(simultaneous())))
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][1], self.origin["session_key"])
        self.assertEqual(self.state(), "scheduled")

    def test_bridge_without_store_or_after_reset_fails_closed(self):
        row, event, hook = self.bridge()
        asyncio.run(hook(event))
        self.assertEqual(self.state(), "failed")
        self.store.add_event(row["watch_id"], "event-2", "gmail", {})
        event.raw_message["event_id"] = "event-2"
        changed = SimpleNamespace(lookup_by_session_key=lambda key: SimpleNamespace(session_id="new-session"))
        asyncio.run(hook(event, session_store=changed))
        self.assertEqual(self.store.outbox(row["watch_id"])[1]["state"], "failed")
        self.assertEqual(self.injected, [])

    def test_bridge_matched_reservation_survives_expiry_only(self):
        row, event, hook = self.bridge()
        with self.store.tx() as conn:
            conn.execute("UPDATE watches SET expires_at=? WHERE watch_id=?", (self.clock.now - 10, row["watch_id"]))
        asyncio.run(hook(event, session_store=self.sessions))
        self.assertEqual(self.injected, [])
        self.store.update_watch(row["watch_id"], state="matched")
        asyncio.run(hook(event, session_store=self.sessions))
        self.assertEqual(self.state(), "scheduled")

    def test_bridge_forged_route_profile_generation_and_unknown_event_suppressed(self):
        row, event, hook = self.bridge()
        mutations = [("source", "chat_name", "webhook/foreign"), ("source", "platform", "discord"),
                     ("source", "user_id", "attacker"), ("source", "profile", "other"),
                     ("raw_message", "generation", 99), ("raw_message", "event_id", "unknown")]
        for target, key, value in mutations:
            forged = copy.deepcopy(event)
            if target == "source":
                setattr(forged.source, key, value)
            else:
                forged.raw_message[key] = value
            self.assertEqual(asyncio.run(hook(forged, session_store=self.sessions))["action"], "skip")
        event.raw_message = None
        self.assertEqual(asyncio.run(hook(event))["action"], "skip")
        self.assertEqual(self.injected, [])
        self.assertEqual(self.state(), "pending")

    def test_bridge_config_change_and_stop_are_not_injected(self):
        row, event, hook = self.bridge()
        self.config["routes"][0]["same_session"]["secret"] = "changed" * 8
        self.assertEqual(asyncio.run(hook(event, session_store=self.sessions))["action"], "skip")
        self.config = config_for(self.origin)
        self.store.stop(row["watch_id"])
        asyncio.run(hook(event, session_store=self.sessions))
        self.assertEqual(self.injected, [])

    def test_injection_refusal_is_uncertain_not_replayed(self):
        row, event, hook = self.bridge()
        self.ctx.inject_message = mock.Mock(return_value=False)
        asyncio.run(hook(event, session_store=self.sessions))
        asyncio.run(hook(event, session_store=self.sessions))
        self.assertEqual(self.state(), "uncertain")
        self.ctx.inject_message.assert_called_once()

    def test_residual_reset_between_lookup_and_inject_is_not_hidden(self):
        row, event, hook = self.bridge()
        observed = {"sid": "session-1"}
        def lookup(key):
            old = SimpleNamespace(session_id=observed["sid"])
            observed["sid"] = "replacement"
            return old
        self.sessions.lookup_by_session_key = lookup
        self.ctx.inject_message = lambda content, *, session_key: self.injected.append(observed["sid"]) or True
        asyncio.run(hook(event, session_store=self.sessions))
        self.assertEqual(self.injected, ["replacement"])
        self.assertEqual(self.state(), "scheduled")  # admission, never a race-free delivery claim


if __name__ == "__main__":
    unittest.main()
