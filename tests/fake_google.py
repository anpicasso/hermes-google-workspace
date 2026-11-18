"""Explicit simulated Google Workspace transport for unit tests.

These tests are not proof of real Google behaviour; they exercise the worker's
state machine and selectors against a deterministic local service model.
"""
from __future__ import annotations

import base64
import copy
import json


def _page(items, token, size):
    offset = int(token or 0)
    chunk = items[offset:offset + size]
    next_token = str(offset + size) if offset + size < len(items) else None
    return chunk, next_token


class FakeGoogle:
    def __init__(self):
        self.gmail = {
            "history_id": "100", "messages": {}, "history": [], "page_size": 100,
            "pubsub": [], "acknowledged": [], "watch_calls": 0, "stop_calls": 0,
            "expiration": "2000000000000",
        }
        self.drive = {
            "files": {}, "start_token": "10", "new_start": "20",
            "changes": [], "page_size": 100, "watch_calls": [], "stop_calls": [],
            "expiration": "2000000000000",
        }
        self.calendar = {
            "calendars": {}, "events": {}, "deltas": [], "sync_token": "sync-1",
            "next_sync": "sync-2", "page_size": 100, "watch_calls": [], "stop_calls": [],
            "expiration": "2000000000000", "gone": False,
        }
        self.scopes_value = ["https://www.googleapis.com/auth/gmail.metadata"]
        self.project_value = None
        self.failures = {}
        self.calls = []

    def project(self, account):
        return self.project_value

    # --- helpers ---------------------------------------------------------
    def fail(self, key, status=500, code="api_error", retryable=True):
        self.failures[key] = {"ok": False, "http_status": status,
                              "error_code": code, "retryable": retryable,
                              "error": "simulated Google failure"}

    def scopes(self, account):
        return self.scopes_value

    def _failure_for(self, key):
        for candidate in (key, key[:3], key[:2], key[:1]):
            if candidate in self.failures:
                return copy.deepcopy(self.failures[candidate])
        return None

    def call(self, account, args, params=None, body=None, timeout=30):
        key = tuple(args)
        self.calls.append({"account": account, "args": list(args),
                           "params": copy.deepcopy(params), "body": copy.deepcopy(body)})
        failure = self._failure_for(key)
        if failure is not None:
            return failure
        if key[:2] == ("gmail", "users"):
            return self._gmail(key, params or {}, body or {})
        if key[:1] == ("pubsub",) or str(args[0]).startswith("pubsub:"):
            return self._pubsub(params or {}, body or {})
        if key[:1] == ("drive",):
            return self._drive(key, params or {}, body or {})
        if key[:1] == ("calendar",):
            return self._calendar(key, params or {}, body or {})
        return {"ok": False, "error_code": "simulated_unknown_call", "retryable": False}

    # --- Gmail -----------------------------------------------------------
    def add_message(self, message_id, thread_id, *, sender="alice@example.com", subject="Hello",
                    labels=None, internal_date="1700000000000", attachments=False):
        parts = []
        if attachments:
            parts.append({"filename": "invoice.pdf", "mimeType": "application/pdf"})
        self.gmail["messages"][message_id] = {
            "id": message_id, "threadId": thread_id,
            "labelIds": labels if labels is not None else ["INBOX"],
            "internalDate": internal_date,
            "payload": {"headers": [{"name": "From", "value": sender},
                                    {"name": "Subject", "value": subject}],
                        "filename": "", "parts": parts},
        }

    def add_history(self, history_id, *message_ids):
        self.gmail["history"].append({
            "historyId": str(history_id),
            "messagesAdded": [{"message": {"id": message_id, "threadId":
                                           self.gmail["messages"][message_id]["threadId"]}}
                              for message_id in message_ids],
        })
        self.gmail["history_id"] = str(max(int(self.gmail["history_id"]), int(history_id)))

    def _gmail(self, key, params, body):
        if key[2:] == ("getProfile",):
            return {"ok": True, "data": {"emailAddress": "person@example.com",
                                         "historyId": self.gmail["history_id"]}}
        if key[2:] == ("messages", "get"):
            message = self.gmail["messages"].get(params.get("id"))
            if message is None:
                return {"ok": False, "http_status": 404, "error_code": "api_error",
                        "retryable": False, "error": "not found"}
            result = copy.deepcopy(message)
            if params.get("format") == "metadata":
                result["payload"] = {"headers": result["payload"]["headers"], "filename": "",
                                     "parts": []}
            return {"ok": True, "data": result}
        if key[2:] == ("threads", "get"):
            thread_id = params.get("id")
            messages = [m for m in self.gmail["messages"].values() if m["threadId"] == thread_id]
            if not messages:
                return {"ok": False, "http_status": 404, "error_code": "api_error",
                        "retryable": False, "error": "not found"}
            return {"ok": True, "data": {"id": thread_id, "messages": messages}}
        if key[2:] == ("history", "list"):
            start = int(params.get("startHistoryId") or 0)
            records = [r for r in self.gmail["history"] if int(r["historyId"]) > start]
            chunk, next_token = _page(records, params.get("pageToken"), self.gmail["page_size"])
            data = {"historyId": self.gmail["history_id"], "history": chunk}
            if next_token:
                data["nextPageToken"] = next_token
            return {"ok": True, "data": data}
        if key[2:] == ("watch",):
            self.gmail["watch_calls"] += 1
            return {"ok": True, "data": {"historyId": self.gmail["history_id"],
                                         "expiration": self.gmail["expiration"]}}
        if key[2:] == ("stop",):
            self.gmail["stop_calls"] += 1
            return {"ok": True, "data": {}}
        return {"ok": False, "error_code": "simulated_unknown_call", "retryable": False}

    def _pubsub(self, params, body):
        if params.get("subscription") is None:
            return {"ok": True, "data": {"receivedMessages": []}}
        batch = self.gmail["pubsub"].pop(0) if self.gmail["pubsub"] else []
        if not batch:
            return {"ok": True, "data": {"receivedMessages": []}}
        return {"ok": True, "data": {"receivedMessages": batch}}

    # --- Drive -----------------------------------------------------------
    def add_file(self, file_id, name, mime_type="text/plain", parents=(), trashed=False,
                 version="1", modified="2026-01-01T00:00:00Z", drive_id=""):
        self.drive["files"][file_id] = {
            "id": file_id, "name": name, "mimeType": mime_type,
            "parents": list(parents), "trashed": trashed, "version": version,
            "modifiedTime": modified, "webViewLink": "https://example.invalid/" + file_id,
            "driveId": drive_id,
        }

    def add_change(self, file_id, *, removed=False, file=None):
        self.drive["changes"].append({"fileId": file_id, "removed": removed,
                                      "file": copy.deepcopy(file) if file else None})

    def set_change_pages(self, changes, page_size=100):
        self.drive["changes"] = changes
        self.drive["page_size"] = page_size

    def _drive(self, key, params, body):
        if key[1:] == ("files", "get"):
            file = self.drive["files"].get(params.get("fileId"))
            if file is None:
                return {"ok": False, "http_status": 404, "error_code": "api_error",
                        "retryable": False, "error": "not found"}
            return {"ok": True, "data": copy.deepcopy(file)}
        if key[1:] == ("files", "list"):
            parent = None
            query = params.get("q", "")
            if "'" in query:
                parent = query.split("'")[1]
            files = [f for f in self.drive["files"].values()
                     if (parent is None or parent in f.get("parents", [])) and not f.get("trashed")]
            chunk, next_token = _page(files, params.get("pageToken"), 1000)
            data = {"files": copy.deepcopy(chunk)}
            if next_token:
                data["nextPageToken"] = next_token
            return {"ok": True, "data": data}
        if key[1:] == ("changes", "getStartPageToken"):
            return {"ok": True, "data": {"startPageToken": self.drive["start_token"]}}
        if key[1:] == ("changes", "watch"):
            self.drive["watch_calls"].append(copy.deepcopy(body))
            return {"ok": True, "data": {"id": body.get("id"),
                                         "resourceId": "drive-resource",
                                         "expiration": self.drive["expiration"]}}
        if key[1:] == ("changes", "list"):
            token = params.get("pageToken")
            if token in (self.drive["start_token"], self.drive["new_start"]):
                token = None
            chunk, next_token = _page(self.drive["changes"], token, self.drive["page_size"])
            data = {"changes": copy.deepcopy(chunk)}
            if next_token:
                data["nextPageToken"] = next_token
            else:
                data["newStartPageToken"] = self.drive["new_start"]
            return {"ok": True, "data": data}
        if key[1:] == ("channels", "stop"):
            self.drive["stop_calls"].append(copy.deepcopy(params))
            return {"ok": True, "data": {}}
        return {"ok": False, "error_code": "simulated_unknown_call", "retryable": False}

    # --- Calendar --------------------------------------------------------
    def add_calendar(self, calendar_id, *, primary=False):
        self.calendar["calendars"][calendar_id] = {"id": calendar_id, "primary": primary}

    def add_event(self, calendar_id, event_id, *, summary="Meeting", status="confirmed",
                  start="2026-06-01T10:00:00Z", end="2026-06-01T11:00:00Z",
                  organizer="alice@example.com", attendees=None, recurring=None,
                  original_start=None, updated="2026-06-01T09:00:00Z",
                  date_start=None, date_end=None):
        start_obj = {"date": date_start, "timeZone": "UTC"} if date_start else {"dateTime": start}
        end_obj = {"date": date_end, "timeZone": "UTC"} if date_end else {"dateTime": end}
        event = {"id": event_id, "status": status, "summary": summary,
                 "location": "", "start": start_obj, "end": end_obj,
                 "organizer": {"email": organizer},
                 "attendees": copy.deepcopy(attendees or []),
                 "recurringEventId": recurring or "",
                 "originalStartTime": original_start or {},
                 "updated": updated}
        self.calendar["events"].setdefault(calendar_id, {})[event_id] = event
        return event

    def _calendar(self, key, params, body):
        if key[1:] == ("calendars", "get"):
            calendar_id = params.get("calendarId")
            if calendar_id == "primary":
                calendar_id = next((cid for cid, value in self.calendar["calendars"].items()
                                    if value.get("primary")), None)
            if calendar_id not in self.calendar["calendars"]:
                return {"ok": False, "http_status": 404, "error_code": "api_error",
                        "retryable": False, "error": "not found"}
            return {"ok": True, "data": copy.deepcopy(self.calendar["calendars"][calendar_id])}
        if key[1:] == ("events", "get"):
            event = self.calendar["events"].get(params.get("calendarId"), {}).get(params.get("eventId"))
            if event is None:
                return {"ok": False, "http_status": 404, "error_code": "api_error",
                        "retryable": False, "error": "not found"}
            return {"ok": True, "data": copy.deepcopy(event)}
        if key[1:] == ("events", "list"):
            if self.calendar["gone"] and params.get("syncToken"):
                return {"ok": False, "http_status": 410, "error_code": "gone", "retryable": False,
                        "error": "gone"}
            if params.get("syncToken"):
                items = copy.deepcopy(self.calendar["deltas"])
                token = self.calendar["next_sync"]
            else:
                items = [copy.deepcopy(event) for event in
                         self.calendar["events"].get(params.get("calendarId"), {}).values()]
                token = self.calendar["sync_token"]
            chunk, next_token = _page(items, params.get("pageToken"), self.calendar["page_size"])
            data = {"items": chunk}
            if next_token:
                data["nextPageToken"] = next_token
            else:
                data["nextSyncToken"] = token
            return {"ok": True, "data": data}
        if key[1:] == ("events", "watch"):
            self.calendar["watch_calls"].append(copy.deepcopy(body))
            return {"ok": True, "data": {"id": body.get("id"), "resourceId": "calendar-resource",
                                         "expiration": self.calendar["expiration"]}}
        if key[1:] == ("channels", "stop"):
            self.calendar["stop_calls"].append(copy.deepcopy(params))
            return {"ok": True, "data": {}}
        return {"ok": False, "error_code": "simulated_unknown_call", "retryable": False}


def pubsub_message(email, history_id):
    raw = json.dumps({"emailAddress": email, "historyId": str(history_id)}).encode()
    return {"ackId": "ack-" + str(history_id),
            "message": {"data": base64.b64encode(raw).decode()}}
