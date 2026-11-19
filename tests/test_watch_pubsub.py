"""Offline PUSH verification with real RSA signatures and official google-auth."""
import asyncio
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt, jwt
from google.auth.transport import requests as google_requests

import watch
import watch_pubsub as P
from watch_worker import Worker, CallbackServer
from fake_google import FakeGoogle as OriginalGoogle
from watch_helpers import runtime as original_runtime, Clock, FakeRoutes, start_gmail

PUSH_CONFIG = {"watch_gmail_push_audience": "https://watch.example.invalid/google/gmail",
               "watch_gmail_push_service_account": "gmail-push@project.iam.gserviceaccount.com"}


def runtime(*args, config=None, **kwargs):
    return original_runtime(*args, config={**PUSH_CONFIG, **(config or {})}, **kwargs)


FakeGoogle = OriginalGoogle

def envelope(address="person@example.com", history=101, **overrides):
    data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": history}).encode()).decode()
    return json.dumps({"subscription": "projects/p/subscriptions/s",
                       "message": {"messageId": "synthetic-101", "data": data}, **overrides}).encode()


class PubSubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        cls.signer = crypt.RSASigner.from_string(pem, key_id="synthetic")
        cls.public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.client, self.routes = FakeGoogle(), FakeRoutes()
        self.rt = runtime(self.store, self.client, self.routes)
        result = start_gmail(self.rt, batch_seconds=0)
        self.assertTrue(result["ok"], result)
        self.row = self.store.get_watch(result["watch"]["watch_id"])
        self.source = self.store.sources_for(self.row["watch_id"])[0]
        # Replace certificate HTTP only, NOT the verifier or JWT decoder.
        def certs(request, url, method="GET", **kwargs):
            self.assertEqual(url, "https://www.googleapis.com/oauth2/v1/certs")
            self.assertEqual(kwargs["timeout"], 10)
            return SimpleNamespace(status=200, data=json.dumps({"synthetic": self.public}).encode())
        self.certs = patch.object(google_requests.Request, "__call__", certs)
        self.certs.start()
        self.addCleanup(self.certs.stop)

    def token(self, **overrides):
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": PUSH_CONFIG["watch_gmail_push_audience"],
                  "email": PUSH_CONFIG["watch_gmail_push_service_account"], "email_verified": True,
                  "sub": "synthetic-service-account", "iat": now - 1, "exp": now + 300}
        return jwt.encode(self.signer, {**claims, **overrides}).decode()

    def push(self, body=None, token=None, config=None):
        return P.process_push(self.store, config or self.rt.config,
                              {"Authorization": "Bearer " + (token or self.token())},
                              envelope() if body is None else body)

    def test_real_signature_durable_replay_restart_and_forward(self):
        self.assertEqual(self.push()[0], 204)
        self.assertEqual(len(self.store.pending_hints()), 1)
        self.store = watch.WatchStore(self.home, clock=self.clock)
        self.assertEqual(self.push()[0], 204)
        self.assertEqual(len(self.store.pending_hints()), 1)
        self.client.add_message("m1", "T1", internal_date="1800000000000")
        self.client.add_history(101, "m1")
        worker = Worker(self.home, self.client, self.routes, self.rt.config,
                        store=self.store, clock=self.clock)
        worker.run_once()
        self.assertEqual(self.store.pending_hints(), [])
        self.assertEqual(len(self.routes.posts), 1)
        self.assertEqual(self.push()[0], 204)
        worker.run_once()
        self.assertEqual(len(self.routes.posts), 1)
        self.assertFalse(any(c["args"][0].startswith("pubsub") for c in self.client.calls))

    def test_signed_wrong_claims_fail_closed(self):
        for claims in ({"aud": "wrong"}, {"iss": "https://evil.invalid"}, {"exp": 1},
                       {"iat": int(time.time()) + 1000}, {"email": "other@project.iam.gserviceaccount.com"},
                       {"email_verified": False}, {"email_verified": "true"}):
            with self.subTest(claims=claims):
                self.assertEqual(self.push(token=self.token(**claims))[0], 403)
        self.assertEqual(self.store.pending_hints(), [])

    def test_signature_tampering_and_missing_auth(self):
        token = self.token()
        parts = token.split('.')
        signature = bytearray(base64.urlsafe_b64decode(parts[2] + '=='))
        signature[0] ^= 1
        parts[2] = base64.urlsafe_b64encode(signature).decode().rstrip('=')
        self.assertEqual(self.push(token='.'.join(parts))[0], 403)
        self.assertEqual(P.process_push(self.store, self.rt.config, {}, envelope())[0], 401)

    def test_config_body_subscription_and_mailbox_boundaries(self):
        for key in (*PUSH_CONFIG, "watch_gmail_topic", "watch_gmail_subscription", "watch_callback_url"):
            self.assertEqual(self.push(config={**self.rt.config, key: ""})[0], 503)
        self.assertEqual(self.push(body=b'x' * (P.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.push(body=envelope(subscription="projects/p/subscriptions/other"))[0], 400)
        self.assertEqual(self.push(body=envelope(address="foreign@example.com"))[0], 404)
        for history in (True, -1, 1.5, "abc", "1" * 41):
            self.assertEqual(self.push(body=envelope(history=history))[0], 400)
        for body in (b'[]', b'{}', b'null', b'{', envelope(message={"messageId": "x", "data": "!!!"})):
            self.assertEqual(self.push(body=body)[0], 400)
        self.assertEqual(self.store.pending_hints(), [])

    def test_database_failure_never_acknowledges(self):
        with patch.object(self.store, "add_hint", side_effect=OSError("disk full")):
            self.assertEqual(self.push()[0], 503)
        with patch.object(self.store, "get_source", side_effect=OSError("database offline")):
            self.assertEqual(self.push()[0], 503)
        self.assertEqual(self.push()[0], 204)

    def test_loopback_http_endpoint_and_body_limit(self):
        import aiohttp
        async def run():
            worker = Worker(self.home, self.client, self.routes, self.rt.config, store=self.store)
            server = CallbackServer(worker, port=0)
            await server.start()
            try:
                port = server.runner.addresses[0][1]
                url = f'http://127.0.0.1:{port}/google/gmail'
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, data=envelope(), headers={"Authorization": "Bearer " + self.token()}) as response:
                        self.assertEqual(response.status, 204)
                    async with session.post(url, data=b'x' * (P.MAX_BODY + 1)) as response:
                        self.assertEqual(response.status, 413)
            finally:
                await server.stop()
            with self.assertRaises(ValueError):
                await CallbackServer(worker, host="0.0.0.0", port=0).start()
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
