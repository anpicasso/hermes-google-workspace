import base64
import email
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from email import policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import workspace_tools as T

A = "me@example.com"


def params(argv):
    return json.loads(argv[argv.index("--params") + 1]) if "--params" in argv else None


def body(argv):
    return json.loads(argv[argv.index("--json") + 1]) if "--json" in argv else None


def mail(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


VALID = {
    "gws_gmail_search": {"account": A, "query": "is:unread"},
    "gws_gmail_read": {"account": A, "message_id": "18abc"},
    "gws_gmail_draft": {"account": A, "to": ["b@example.com"], "subject": "Hi", "body": "x"},
    "gws_gmail_send": {"account": A, "to": ["b@example.com"], "subject": "Hi", "body": "x"},
    "gws_drive_search": {"account": A, "query": "trashed=false"},
    "gws_drive_read": {"account": A, "file_id": "f1"},
    "gws_drive_upload": {"account": A, "path": "up.txt"},
    "gws_drive_share": {"account": A, "file_id": "f1", "role": "reader", "type": "user",
                        "email_address": "b@example.com"},
    "gws_calendar_list": {"account": A},
    "gws_calendar_freebusy": {"account": A, "time_min": "2026-01-01T00:00:00Z",
                              "time_max": "2026-01-02T00:00:00Z"},
    "gws_calendar_create": {"account": A, "summary": "S", "start": "2026-01-01T10:00:00-05:00",
                            "end": "2026-01-01T11:00:00-05:00"},
    "gws_calendar_update": {"account": A, "event_id": "e1", "summary": "S2"},
    "gws_schema": {"account": A, "method": "gmail.users.messages.send"},
}


class SchemaTests(unittest.TestCase):
    def test_every_tool_has_strict_schema_and_builds(self):
        self.assertEqual(set(SCHEMAS := T.SCHEMAS), set(VALID))
        for name, schema in SCHEMAS.items():
            p = schema["parameters"]
            self.assertEqual(schema["name"], name)
            self.assertFalse(p["additionalProperties"])
            self.assertEqual(p["required"][0], "account")
            argv = T.build_args(name, VALID[name])
            self.assertTrue(argv and all(isinstance(x, str) for x in argv))
        self.assertLessEqual(T.WRITE_TOOLS, set(SCHEMAS))

    def test_deterministic(self):
        for name, args in VALID.items():
            if name in ("gws_gmail_draft", "gws_gmail_send"):
                continue  # MIME carries no Date/Message-ID, but compare decoded anyway below
            self.assertEqual(T.build_args(name, dict(args)), T.build_args(name, dict(args)))
        a = T.build_args("gws_gmail_send", VALID["gws_gmail_send"])
        self.assertEqual(a, T.build_args("gws_gmail_send", VALID["gws_gmail_send"]))

    def test_common_rejections(self):
        for name, args in VALID.items():
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                T.build_args(name, {**args, "extra": 1})
            with self.assertRaisesRegex(ValueError, "missing"):
                T.build_args(name, {k: v for k, v in args.items() if k != "account"})
            for bad in ("..", "-x", "a b", "a\nb", 5, "x" * 129):
                with self.assertRaises(ValueError):
                    T.build_args(name, {**args, "account": bad})
        with self.assertRaisesRegex(ValueError, "unknown tool"):
            T.build_args("gws_api", {"account": A})
        with self.assertRaisesRegex(ValueError, "object"):
            T.build_args("gws_schema", ["x"])

    def test_type_bounds(self):
        s = VALID["gws_gmail_search"]
        for bad in (0, 101, True, "5", 1.0):
            with self.assertRaises(ValueError):
                T.build_args("gws_gmail_search", {**s, "max_results": bad})
        with self.assertRaises(ValueError):
            T.build_args("gws_gmail_search", {**s, "include_spam_trash": 1})
        with self.assertRaises(ValueError):
            T.build_args("gws_gmail_search", {**s, "query": "x\ny"})
        with self.assertRaises(ValueError):
            T.build_args("gws_gmail_search", {**s, "query": "x" * 2049})
        with self.assertRaises(ValueError):
            T.build_args("gws_gmail_search", {**s, "page_token": "a b"})


class GmailTests(unittest.TestCase):
    def test_search_and_read(self):
        argv = T.build_args("gws_gmail_search", {**VALID["gws_gmail_search"], "max_results": 5,
                                                 "page_token": "tok"})
        self.assertEqual(argv[:4], ["gmail", "users", "messages", "list"])
        self.assertEqual(params(argv), {"userId": "me", "q": "is:unread", "maxResults": 5,
                                        "pageToken": "tok", "includeSpamTrash": False})
        argv = T.build_args("gws_gmail_read", {"account": A, "message_id": "m1", "format": "metadata",
                                               "metadata_headers": ["From", "Subject"]})
        self.assertEqual(params(argv), {"userId": "me", "id": "m1", "format": "metadata",
                                        "metadataHeaders": ["From", "Subject"]})
        with self.assertRaisesRegex(ValueError, "metadata"):
            T.build_args("gws_gmail_read", {"account": A, "message_id": "m1", "metadata_headers": ["From"]})
        for bad in ("../x", "a/b", ""):
            with self.assertRaises(ValueError):
                T.build_args("gws_gmail_read", {"account": A, "message_id": bad})
        with self.assertRaises(ValueError):
            T.build_args("gws_gmail_read", {"account": A, "message_id": "m", "format": "html"})

    def test_send_builds_mime(self):
        argv = T.build_args("gws_gmail_send", {
            "account": A, "to": ["b@example.com", "c@example.com"], "cc": ["d@example.com"],
            "bcc": ["e@example.com"], "subject": "Año ñ", "body": "línea 1\nlínea 2\n",
            "thread_id": "t1", "in_reply_to": "<abc@mail>"})
        self.assertEqual(argv[:4], ["gmail", "users", "messages", "send"])
        self.assertEqual(params(argv), {"userId": "me"})
        b = body(argv)
        self.assertEqual(b["threadId"], "t1")
        m = mail(b["raw"])
        self.assertEqual(m["To"], "b@example.com, c@example.com")
        self.assertEqual(m["Cc"], "d@example.com")
        self.assertEqual(m["Bcc"], "e@example.com")
        self.assertEqual(m["Subject"], "Año ñ")
        self.assertEqual(m["In-Reply-To"], "<abc@mail>")
        self.assertEqual(m.get_content().replace("\r\n", "\n"), "línea 1\nlínea 2\n")
        self.assertNotIn("=", b["raw"].rstrip("="))  # base64url alphabet only
        self.assertNotIn("+", b["raw"])

    def test_draft(self):
        argv = T.build_args("gws_gmail_draft", {"account": A})
        self.assertEqual(argv[:4], ["gmail", "users", "drafts", "create"])
        self.assertIn("raw", body(argv)["message"])

    def test_header_injection_and_recipients(self):
        base = VALID["gws_gmail_send"]
        for field, value in (("subject", "Hi\r\nBcc: evil@x.com"), ("subject", "Hi\nX: y"),
                             ("subject", "a\x00"), ("to", ["b@example.com\nBcc: e@x.com"]),
                             ("to", ["b@example.com, evil@x.com"]), ("to", ["Name <b@x.com>"]),
                             ("cc", ["not-an-email"]), ("in_reply_to", "<a>\r\nBcc: e@x.com"),
                             ("in_reply_to", "abc"), ("body", "x\ry"), ("to", "b@example.com"),
                             ("to", ["a@b.com"] * 101), ("thread_id", "t 1")):
            with self.assertRaises(ValueError, msg=(field, value)):
                T.build_args("gws_gmail_send", {**base, field: value})
        with self.assertRaisesRegex(ValueError, "recipient"):
            T.build_args("gws_gmail_send", {"account": A, "subject": "s", "body": "b"})
        with self.assertRaisesRegex(ValueError, "missing"):
            T.build_args("gws_gmail_send", {"account": A, "to": ["b@example.com"], "body": "b"})

    def test_body_tabs_allowed_and_size_cap(self):
        T.build_args("gws_gmail_send", {**VALID["gws_gmail_send"], "body": "a\tb\nc"})
        with self.assertRaises(ValueError):
            T.build_args("gws_gmail_send", {**VALID["gws_gmail_send"], "body": "x" * 60_001})
        # 60k multibyte chars overflow the single argv element cap.
        with self.assertRaisesRegex(ValueError, "too large"):
            T.build_args("gws_gmail_send", {**VALID["gws_gmail_send"], "body": "€" * 60_000})


class DriveTests(unittest.TestCase):
    def test_search(self):
        argv = T.build_args("gws_drive_search", VALID["gws_drive_search"])
        self.assertEqual(argv[:3], ["drive", "files", "list"])
        p = params(argv)
        self.assertEqual((p["q"], p["pageSize"], p["corpora"]), ("trashed=false", 20, "allDrives"))
        self.assertTrue(p["fields"].startswith("incompleteSearch,nextPageToken,files("))
        p = params(T.build_args("gws_drive_search", {"account": A, "include_shared_drives": False}))
        self.assertNotIn("corpora", p)
        self.assertNotIn("incompleteSearch", p["fields"])
        self.assertNotIn("q", p)

    def test_read_modes(self):
        argv = T.build_args("gws_drive_read", VALID["gws_drive_read"])
        self.assertEqual(argv[:3], ["drive", "files", "get"])
        self.assertIn("fields", params(argv))
        argv = T.build_args("gws_drive_read", {"account": A, "file_id": "f1", "mode": "content",
                                               "output_path": "dl/out.bin"})
        self.assertEqual(params(argv)["alt"], "media")
        self.assertEqual(argv[-2:], ["--output", "dl/out.bin"])
        argv = T.build_args("gws_drive_read", {"account": A, "file_id": "f1", "mode": "export",
                                               "mime_type": "text/plain", "output_path": "doc.txt"})
        self.assertEqual(argv[:3], ["drive", "files", "export"])
        self.assertEqual(params(argv), {"fileId": "f1", "mimeType": "text/plain"})
        self.assertEqual(argv[-2:], ["--output", "doc.txt"])
        for bad in ({"mode": "export", "output_path": "x"}, {"mime_type": "text/plain"},
                    {"mode": "content"}, {"mode": "export", "mime_type": "text/plain"},
                    {"output_path": "x"}, {"mode": "content", "output_path": "../x"},
                    {"mode": "content", "output_path": "/etc/passwd"},
                    {"mode": "content", "output_path": "-x"},
                    {"mode": "content", "output_path": "a/../../b"},
                    {"mode": "export", "mime_type": "text", "output_path": "x"}, {"file_id": "a/b"}):
            with self.assertRaises(ValueError, msg=bad):
                T.build_args("gws_drive_read", {"account": A, "file_id": "f1", **bad})

    def test_upload(self):
        argv = T.build_args("gws_drive_upload", {"account": A, "path": "docs/r.pdf", "parent_id": "p1",
                                                 "mime_type": "application/pdf"})
        self.assertEqual(argv[:3], ["drive", "files", "create"])
        self.assertEqual(body(argv), {"name": "r.pdf", "parents": ["p1"]})
        self.assertEqual(argv[-4:], ["--upload", "docs/r.pdf", "--upload-content-type", "application/pdf"])
        self.assertEqual(body(T.build_args("gws_drive_upload", {"account": A, "path": "a", "name": "B"}))["name"], "B")
        for bad in ("../x", "/etc/passwd", "--upload", "a/../../x", "a\nb", "..", "a;b"):
            with self.assertRaises(ValueError, msg=bad):
                T.build_args("gws_drive_upload", {"account": A, "path": bad})

    def test_share(self):
        argv = T.build_args("gws_drive_share", {**VALID["gws_drive_share"], "message": "hola\nmundo"})
        self.assertEqual(argv[:3], ["drive", "permissions", "create"])
        self.assertEqual(body(argv), {"role": "reader", "type": "user", "emailAddress": "b@example.com"})
        self.assertEqual(params(argv)["sendNotificationEmail"], True)
        self.assertEqual(params(argv)["emailMessage"], "hola\nmundo")
        argv = T.build_args("gws_drive_share", {"account": A, "file_id": "f", "role": "writer",
                                                "type": "domain", "domain": "example.com"})
        self.assertEqual(body(argv), {"role": "writer", "type": "domain", "domain": "example.com"})
        self.assertNotIn("sendNotificationEmail", params(argv))
        base = VALID["gws_drive_share"]
        for bad in ({"role": "owner"}, {"type": "anyone"}, {"type": "domain"},
                    {"domain": "example.com"}, {"send_notification": False, "message": "x"},
                    {"email_address": "x"}):
            with self.assertRaises(ValueError, msg=bad):
                T.build_args("gws_drive_share", {**base, **bad})
        with self.assertRaises(ValueError):
            T.build_args("gws_drive_share", {k: v for k, v in base.items() if k != "email_address"})


class CalendarTests(unittest.TestCase):
    def test_list(self):
        argv = T.build_args("gws_calendar_list", {"account": A, "time_min": "2026-01-01T00:00:00Z",
                                                  "time_max": "2026-01-02T00:00:00-05:00", "query": "x"})
        self.assertEqual(argv[:3], ["calendar", "events", "list"])
        self.assertEqual(params(argv), {"calendarId": "primary", "maxResults": 25, "singleEvents": True,
                                        "orderBy": "startTime", "timeMin": "2026-01-01T00:00:00Z",
                                        "timeMax": "2026-01-02T00:00:00-05:00", "q": "x"})
        for bad in ({"time_min": "2026-01-01"}, {"time_min": "2026-01-01T00:00:00"},
                    {"time_min": "tomorrow"}, {"time_min": "2026-13-01T00:00:00Z"},
                    {"time_min": "2026-01-02T00:00:00Z", "time_max": "2026-01-01T00:00:00Z"},
                    {"calendar_id": "a/b"}, {"max_results": 251}):
            with self.assertRaises(ValueError, msg=bad):
                T.build_args("gws_calendar_list", {"account": A, **bad})

    def test_freebusy(self):
        argv = T.build_args("gws_calendar_freebusy", {**VALID["gws_calendar_freebusy"],
                                                      "calendar_ids": ["primary", "x@group.calendar.google.com"],
                                                      "time_zone": "America/Lima"})
        self.assertEqual(argv[:3], ["calendar", "freebusy", "query"])
        self.assertIsNone(params(argv))
        self.assertEqual(body(argv)["items"], [{"id": "primary"}, {"id": "x@group.calendar.google.com"}])
        self.assertEqual(body(argv)["timeZone"], "America/Lima")
        self.assertEqual(body(T.build_args("gws_calendar_freebusy", VALID["gws_calendar_freebusy"]))["items"],
                         [{"id": "primary"}])
        with self.assertRaises(ValueError):
            T.build_args("gws_calendar_freebusy", {**VALID["gws_calendar_freebusy"], "calendar_ids": []})

    def test_create(self):
        argv = T.build_args("gws_calendar_create", {**VALID["gws_calendar_create"], "time_zone": "America/Lima",
                                                    "attendees": ["b@example.com"], "description": "a\nb"})
        self.assertEqual(argv[:3], ["calendar", "events", "insert"])
        self.assertEqual(params(argv), {"calendarId": "primary", "sendUpdates": "none"})
        b = body(argv)
        self.assertEqual(b["start"], {"dateTime": "2026-01-01T10:00:00-05:00", "timeZone": "America/Lima"})
        self.assertEqual(b["attendees"], [{"email": "b@example.com"}])
        b = body(T.build_args("gws_calendar_create", {"account": A, "summary": "D",
                                                      "start": "2026-01-01", "end": "2026-01-02"}))
        self.assertEqual((b["start"], b["end"]), ({"date": "2026-01-01"}, {"date": "2026-01-02"}))
        base = VALID["gws_calendar_create"]
        for bad in ({"end": "2026-01-01T09:00:00-05:00"}, {"end": "2026-01-02"},
                    {"start": "2026-01-01", "end": "2026-01-01"}, {"start": "2026-02-30", "end": "2026-03-01"},
                    {"send_updates": "everyone"}, {"summary": "a\nb"}, {"time_zone": "../x"}):
            with self.assertRaises(ValueError, msg=bad):
                T.build_args("gws_calendar_create", {**base, **bad})

    def test_update(self):
        argv = T.build_args("gws_calendar_update", {**VALID["gws_calendar_update"], "send_updates": "all"})
        self.assertEqual(argv[:3], ["calendar", "events", "patch"])
        self.assertEqual(params(argv), {"calendarId": "primary", "eventId": "e1", "sendUpdates": "all"})
        self.assertEqual(body(argv), {"summary": "S2"})
        self.assertEqual(body(T.build_args("gws_calendar_update", {"account": A, "event_id": "e1",
                                                                   "start": "2026-01-01T10:00:00Z"})),
                         {"start": {"dateTime": "2026-01-01T10:00:00Z"}})
        for bad in ({}, {"time_zone": "UTC"}, {"event_id": "a/b", "summary": "x"}):
            with self.assertRaises(ValueError, msg=bad):
                T.build_args("gws_calendar_update", {"account": A, "event_id": "e1", **bad})


class SchemaToolTests(unittest.TestCase):
    def test_schema(self):
        self.assertEqual(T.build_args("gws_schema", VALID["gws_schema"]), ["schema", "gmail.users.messages.send"])
        self.assertEqual(T.build_args("gws_schema", {**VALID["gws_schema"], "resolve_refs": True})[-1],
                         "--resolve-refs")
        for bad in ("gmail", "--help", "gmail.users;rm", "Gmail.x", "a..b"):
            with self.assertRaises(ValueError, msg=bad):
                T.build_args("gws_schema", {"account": A, "method": bad})


@unittest.skipUnless(shutil.which("gws"), "native gws not installed")
class NativeDryRunTests(unittest.TestCase):
    """Native gws --dry-run validates our argv offline against its discovery docs."""

    def test_native_accepts_every_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "up.txt").write_text("hi")
            env = {k: v for k, v in os.environ.items() if not k.startswith("GOOGLE_")}
            env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(Path(tmp, "cfg"))
            cases = [(n, a) for n, a in VALID.items() if n != "gws_schema"]  # schema has no --dry-run
            cases += [("gws_drive_read", {"account": A, "file_id": "f1", "mode": "content", "output_path": "out.bin"}),
                      ("gws_drive_read", {"account": A, "file_id": "f1", "mode": "export",
                                          "mime_type": "text/plain", "output_path": "out.txt"})]
            for name, args in cases:
                argv = T.build_args(name, args) + ["--dry-run"]
                r = subprocess.run(["gws", *argv], cwd=tmp, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, (name, r.stdout[-500:], r.stderr[-500:]))
                self.assertTrue(json.loads(r.stdout)["dry_run"])


if __name__ == "__main__":
    unittest.main()
