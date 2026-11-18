import asyncio
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import watch
import watch_routes
from fake_google import FakeGoogle
from watch_helpers import Clock, FakeRoutes, runtime, start_gmail


class Conflict(RuntimeError):
    pass


class FakeWebhook(ModuleType):
    def __init__(self):
        super().__init__("fake_webhook")
        self.store = {}
        self.static = {}
        self.MISSING = object()
        self.SubscriptionMutationConflict = Conflict
        self._MISSING_SUBSCRIPTION = self.MISSING

    def _load_subscriptions(self):
        return json.loads(json.dumps(self.store))

    def _replace_subscription(self, name, route, expected):
        current = self.store.get(name, self.MISSING)
        if (current is self.MISSING) != (expected is self.MISSING) or (
                current is not self.MISSING and current != expected):
            raise Conflict("changed")
        self.store[name] = json.loads(json.dumps(route))
        return route

    def _mutate_subscriptions(self, mutate):
        return mutate(self.store)

    def _route_url(self, name, route):
        return "http://127.0.0.1:8644/webhooks/" + name

    def _get_webhook_config(self):
        return {"extra": {"routes": dict(self.static)}}


class DummyResponse:
    def __init__(self, payload, status=202):
        self.payload = payload
        self.status = status

    def read(self, limit=None):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.client = FakeGoogle()
        self.routes = FakeRoutes(same_session=True)
        self.rt = runtime(self.store, self.client, self.routes)
        self.rt.ctx = SimpleNamespace(inject_message=lambda content, *, session_key=None: True)
        self.fake = FakeWebhook()
        self.module_patch = mock.patch.object(watch_routes, "_module", return_value=self.fake)
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()
        self.tmp.cleanup()

    def _manager(self, ctx=None):
        manager = watch_routes.RouteManager(ctx or SimpleNamespace())
        manager._check_store = lambda: None
        return manager

    def _watch(self, spec=None):
        row = {
            "watch_id": "a" * 16, "service": "gmail", "account": "person@example.com",
            "generation": 1,
        }
        spec = spec or {"instructions": "Summarize it", "skills": [], "mode": "new_session",
                        "account": "person@example.com"}
        origin = {"profile": "default", "platform": "discord", "chat_id": "c1",
                  "thread_id": "t1", "principal": "u1", "session_id": "s1"}
        return row, spec, origin

    def test_same_session_capability_requires_callable_injection(self):
        self.assertFalse(watch_routes.same_session_capability(SimpleNamespace())["supported"])
        self.assertFalse(watch_routes.same_session_capability(
            SimpleNamespace(inject_message=None))["supported"])
        capability = watch_routes.same_session_capability(
            SimpleNamespace(inject_message=lambda content, *, session_key=None: True))
        self.assertTrue(capability["supported"])
        self.assertEqual(capability["keyword"], "session_key")

    def test_new_session_route_is_immutable_native_contract(self):
        row, spec, origin = self._watch()
        manager = self._manager()
        public = manager.create(row, spec, origin)
        route = self.fake.store[public["name"]]
        self.assertEqual(route["description"], "gws-watch owned route watch=" + row["watch_id"] + " mode=new_session")
        self.assertEqual(route["events"], ["gws-watch"])
        self.assertEqual(route["deliver"], "discord")
        self.assertEqual(route["deliver_extra"], {"chat_id": "c1", "thread_id": "t1"})
        self.assertEqual(route["profile"], "default")
        self.assertEqual(route["signature"], "generic")
        self.assertIs(route["deliver_only"], False)
        self.assertIs(route["mirror_to_session"], False)
        self.assertNotIn("cron_job", route)
        self.assertNotIn("coalesce", route)
        self.assertIn(row["watch_id"], route["prompt"])
        self.assertIn("{__raw__}", route["prompt"])

    def test_same_session_route_is_bridge_without_event_prompt(self):
        row, spec, origin = self._watch({"instructions": "secret private body", "skills": [],
                                         "mode": "same_session", "account": "person@example.com"})
        manager = self._manager(SimpleNamespace(inject_message=lambda content, *, session_key=None: True))
        public = manager.create(row, spec, origin)
        route = self.fake.store[public["name"]]
        self.assertTrue(public["name"].startswith("gws-watch-bridge-"))
        self.assertEqual(route["deliver"], "log")
        self.assertIn("NO_REPLY", route["prompt"])
        self.assertNotIn("secret private body", route["prompt"])
        self.assertEqual(route["events"], ["gws-watch-bridge"])

    def test_static_and_foreign_routes_are_never_shadowed(self):
        row, spec, origin = self._watch()
        manager = self._manager()
        self.fake.static["gws-watch-" + row["watch_id"]] = {"x": 1}
        with self.assertRaises(watch_routes.RouteError) as raised:
            manager.create(row, spec, origin)
        self.assertEqual(raised.exception.code, "native_route_static_conflict")
        self.fake.static.clear()
        self.fake.store["gws-watch-" + row["watch_id"]] = {"description": "foreign"}
        with self.assertRaises(watch_routes.RouteError) as raised:
            manager.create(row, spec, origin)
        self.assertEqual(raised.exception.code, "native_route_conflict")
        self.assertEqual(self.fake.store["gws-watch-" + row["watch_id"]], {"description": "foreign"})

    def test_operator_disabled_route_is_preserved(self):
        row, spec, origin = self._watch()
        manager = self._manager()
        public = manager.create(row, spec, origin)
        self.fake.store[public["name"]]["enabled"] = False
        result = manager.remove(public["name"], row["watch_id"])
        self.assertEqual(result["state"], "preserved_disabled")
        self.assertIn(public["name"], self.fake.store)
        self.assertIn("enabled", self.fake.store[public["name"]])

    def test_foreign_and_edited_routes_are_not_removed(self):
        row, spec, origin = self._watch()
        manager = self._manager()
        public = manager.create(row, spec, origin)
        self.fake.store[public["name"]]["description"] = "operator edited"
        self.assertEqual(manager.remove(public["name"], row["watch_id"])["state"], "foreign")
        self.assertIn(public["name"], self.fake.store)
        self.fake.store[public["name"]]["description"] = (
            "gws-watch owned route watch=" + row["watch_id"] + " mode=new_session")
        self.fake.store[public["name"]]["prompt"] = "operator changed prompt"
        self.assertEqual(manager.remove(public["name"], row["watch_id"])["state"], "conflict")
        self.assertIn(public["name"], self.fake.store)

    def test_corrupt_store_refuses_mutation(self):
        path = self.home / "webhook_subscriptions.json"
        path.write_text("{not-json")
        manager = watch_routes.RouteManager(SimpleNamespace())
        manager._store_path = lambda: path
        with self.assertRaises(watch_routes.RouteError) as raised:
            manager._check_store()
        self.assertEqual(raised.exception.code, "native_route_store_corrupt")
        path.write_text("[]")
        with self.assertRaises(watch_routes.RouteError) as raised:
            manager._check_store()
        self.assertEqual(raised.exception.code, "native_route_store_corrupt")

    def test_hmac_v2_and_stable_request_id(self):
        row, spec, origin = self._watch()
        manager = self._manager()
        public = manager.create(row, spec, origin)
        captured = {}

        def fake_urlopen(request, timeout=10):
            captured["request"] = request
            return DummyResponse({"status": "accepted", "delivery_id": "x"})

        with mock.patch("watch_routes.urllib.request.urlopen", side_effect=fake_urlopen):
            result = manager.post(public["name"], {"events": [{"id": "e1"}]}, "request-1")
        self.assertTrue(result["ok"])
        request = captured["request"]
        body = request.data
        timestamp = request.get_header("X-webhook-timestamp")
        expected = hmac.new(manager.secret(public["name"]).encode(),
                            timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-webhook-signature-v2"), expected)
        self.assertEqual(request.get_header("X-request-id"), "request-1")

    def test_post_requires_an_admitted_receipt(self):
        row, spec, origin = self._watch()
        manager = self._manager()
        public = manager.create(row, spec, origin)
        with mock.patch("watch_routes.urllib.request.urlopen") as urlopen:
            urlopen.return_value = DummyResponse({"status": "ignored"}, status=202)
            unconfirmed = manager.post(public["name"], {"events": []}, "request-2")
            urlopen.return_value = DummyResponse({"status": "duplicate"}, status=200)
            duplicate = manager.post(public["name"], {"events": []}, "request-2")
        self.assertFalse(unconfirmed["ok"])
        self.assertTrue(unconfirmed["retryable"])
        self.assertEqual(unconfirmed["error_code"], "native_route_unconfirmed")
        self.assertTrue(duplicate["ok"])
        self.assertTrue(duplicate["duplicate"])

    def test_post_refuses_operator_edited_route_without_sending(self):
        row, spec, origin = self._watch()
        manager = self._manager()
        public = manager.create(row, spec, origin)
        self.fake.store[public["name"]]["deliver_extra"]["chat_id"] = "attacker-chat"
        with mock.patch("watch_routes.urllib.request.urlopen") as urlopen:
            result = manager.post(public["name"], {"events": []}, "request-3")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "native_route_edited")
        self.assertFalse(result["retryable"])
        urlopen.assert_not_called()

    def test_operator_disabled_route_can_be_read_but_not_enabled(self):
        row, spec, origin = self._watch()
        self.fake.store["gws-watch-" + row["watch_id"]] = {
            "description": "gws-watch owned route watch=" + row["watch_id"] + " mode=new_session",
            "enabled": False, "secret": "s", "signature": "generic",
            "deliver": "discord", "profile": "default", "watch_digest": "stale",
        }
        with self.assertRaises(watch_routes.RouteError) as raised:
            self._manager().create(row, spec, origin)
        self.assertEqual(raised.exception.code, "native_route_disabled")
        self.assertFalse(self.fake.store["gws-watch-" + row["watch_id"]]["enabled"])

    def test_start_same_session_fails_closed_without_pin(self):
        no_pin = runtime(self.store, self.client, self.routes)
        no_pin.ctx = SimpleNamespace()
        result = start_gmail(no_pin, target={"mailbox": True}, mode="same_session")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "same_session_host_unsupported")
        self.assertEqual(self.store.list_watches(), [])
        self.assertEqual(self.client.calls, [])

    def test_bridge_hook_consumes_only_owned_route_and_verifies_session(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        started = start_gmail(self.rt, target={"thread_ids": ["T1"]}, mode="same_session")
        self.assertTrue(started["ok"], started)
        row = self.store.get_watch(started["watch"]["watch_id"])
        self.assertTrue(self.store.add_event(row["watch_id"], "event-1", "gmail", {"x": 1}))
        injected = {}
        ctx = SimpleNamespace(inject_message=lambda content, *, session_key=None: injected.update(
            {"content": content, "session_key": session_key}) or True)
        sessions = SimpleNamespace(
            lookup_by_session_key=lambda key: SimpleNamespace(session_id="session-1"))
        hook = watch_routes.bridge_hook(self.store, ctx)
        event = SimpleNamespace(raw_message={"event_type": "gws-watch-bridge",
                                             "watch_id": row["watch_id"],
                                             "event_id": "event-1", "generation": row["generation"]},
                                source=SimpleNamespace(chat_name="webhook/" + row["bridge_name"]))
        result = asyncio.run(hook(event, session_store=sessions))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(injected["session_key"], "agent:default:discord:chat-1:thread-1")
        self.assertNotIn("expected_session_id", injected)
        self.assertEqual(self.store.outbox(row["watch_id"])[0]["state"], "scheduled")
        # A duplicate gateway delivery must not inject twice.
        asyncio.run(hook(event, session_store=sessions))
        self.assertEqual(self.store.outbox(row["watch_id"])[0]["state"], "scheduled")
        self.assertEqual(self.store.delivery_summary(row["watch_id"])["counts"],
                         {"scheduled": 1})

    def test_bridge_hook_fails_closed_without_store_or_on_session_change(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        started = start_gmail(self.rt, target={"thread_ids": ["T1"]}, mode="same_session")
        self.assertTrue(started["ok"], started)
        row = self.store.get_watch(started["watch"]["watch_id"])
        ctx = SimpleNamespace(inject_message=lambda *args, **kwargs: self.fail("must not inject"))
        hook = watch_routes.bridge_hook(self.store, ctx)

        def event(event_id):
            return SimpleNamespace(
                raw_message={"event_type": "gws-watch-bridge", "watch_id": row["watch_id"],
                             "event_id": event_id, "generation": row["generation"]},
                source=SimpleNamespace(chat_name="webhook/" + row["bridge_name"]))

        self.store.add_event(row["watch_id"], "event-1", "gmail", {"x": 1})
        asyncio.run(hook(event("event-1")))
        self.assertEqual(self.store.outbox(row["watch_id"])[0]["state"], "failed")
        self.store.add_event(row["watch_id"], "event-2", "gmail", {"x": 2})
        changed = SimpleNamespace(lookup_by_session_key=lambda key: SimpleNamespace(session_id="other"))
        asyncio.run(hook(event("event-2"), session_store=changed))
        rows = {entry["event_id"]: entry for entry in self.store.outbox(row["watch_id"])}
        self.assertEqual(rows["event-2"]["state"], "failed")
        self.assertIn("changed", rows["event-2"]["detail"])

    def test_bridge_hook_allows_matched_reservation_after_expiry_only(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        started = start_gmail(self.rt, target={"thread_ids": ["T1"]}, mode="same_session")
        self.assertTrue(started["ok"], started)
        row = self.store.get_watch(started["watch"]["watch_id"])
        injected = []
        ctx = SimpleNamespace(inject_message=lambda content, *, session_key=None: injected.append(1) or True)
        sessions = SimpleNamespace(
            lookup_by_session_key=lambda key: SimpleNamespace(session_id="session-1"))
        hook = watch_routes.bridge_hook(self.store, ctx)

        def event(event_id):
            return SimpleNamespace(
                raw_message={"event_type": "gws-watch-bridge", "watch_id": row["watch_id"],
                             "event_id": event_id, "generation": row["generation"]},
                source=SimpleNamespace(chat_name="webhook/" + row["bridge_name"]))

        self.store.add_event(row["watch_id"], "event-1", "gmail", {"x": 1})
        with self.store.tx() as conn:
            conn.execute("UPDATE watches SET expires_at=? WHERE watch_id=?",
                         (self.clock.now - 10, row["watch_id"]))
        asyncio.run(hook(event("event-1"), session_store=sessions))
        self.assertEqual(injected, [])  # active + expired is refused
        self.assertEqual(self.store.outbox(row["watch_id"])[0]["state"], "pending")
        self.store.update_watch(row["watch_id"], state="matched")
        asyncio.run(hook(event("event-1"), session_store=sessions))
        self.assertEqual(injected, [1])  # matched reservation survives its lifetime
        self.assertEqual(self.store.outbox(row["watch_id"])[0]["state"], "scheduled")

    def test_bridge_hook_skips_stale_generation_and_never_falls_through(self):
        self.client.add_message("root", "T1", internal_date="1600000000000")
        started = start_gmail(self.rt, target={"thread_ids": ["T1"]}, mode="same_session")
        self.assertTrue(started["ok"], started)
        row = self.store.get_watch(started["watch"]["watch_id"])
        ctx = SimpleNamespace(inject_message=lambda *args, **kwargs: self.fail("must not inject"))
        hook = watch_routes.bridge_hook(self.store, ctx)
        event = SimpleNamespace(raw_message={"event_type": "gws-watch-bridge",
                                             "watch_id": row["watch_id"], "event_id": "missing",
                                             "generation": 99},
                                source=SimpleNamespace(chat_name="webhook/" + row["bridge_name"]))
        result = asyncio.run(hook(event))
        self.assertEqual(result["action"], "skip")


if __name__ == "__main__":
    unittest.main()
