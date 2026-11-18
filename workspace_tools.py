"""Typed common Gmail/Drive/Calendar tools that compile to native gws argv.

``SCHEMAS`` maps tool name -> tool schema. ``build_args(name, args)`` validates
``args`` strictly against that schema and returns the exact argv list (after the
``gws`` executable) for ``router.execute_gws(root, args["account"], argv)``.
It raises ``ValueError`` on anything malformed; it never touches credentials
or the network. Everything else stays on the generic ``gws_api`` tool.
"""
from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime
from email.message import EmailMessage
from email.policy import SMTP

if __package__:
    from .router import _ACCOUNT
else:  # Direct module use by the standalone test suite.
    from router import _ACCOUNT  # type: ignore

# Linux MAX_ARG_STRLEN is 128 KiB per argv element; stay under it.
MAX_ARG_BYTES = 120_000
# Tools that change remote state or send data; everything else is read-only.
WRITE_TOOLS = frozenset({"gws_gmail_draft", "gws_gmail_send", "gws_drive_upload",
                         "gws_drive_share", "gws_calendar_create", "gws_calendar_update"})

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_CTRL_MULTILINE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # allows \t and \n
_MULTILINE = {"body", "description", "message"}
_EMAIL = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+"
_ID = r"[A-Za-z0-9_-]{1,256}"
_TOKEN = r"[A-Za-z0-9_.~=+/-]{1,2048}"
_CAL_ID = r"[A-Za-z0-9._@+#-]{1,254}"
_REL_PATH = r"(?!-)(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._ /+-]{1,1024}"
_MIME = r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,127}"

ACCOUNT = {"type": "string", "maxLength": 128, "pattern": _ACCOUNT.pattern.removesuffix(r"\Z"),
           "description": "Exact email of an account authorized in this profile"}
_S = lambda n, d, **kw: {"type": "string", "maxLength": n, "description": d, **kw}  # noqa: E731
_I = lambda lo, hi, default, d: {"type": "integer", "minimum": lo, "maximum": hi,  # noqa: E731
                                 "default": default, "description": d}
_B = lambda default, d: {"type": "boolean", "default": default, "description": d}  # noqa: E731
_EMAILS = lambda d: {"type": "array", "maxItems": 100, "description": d,  # noqa: E731
                     "items": {"type": "string", "maxLength": 254, "pattern": _EMAIL}}
_WHEN = _S(40, "RFC3339 with zone (2026-01-02T10:00:00-05:00), or YYYY-MM-DD for all-day")
_PAGE = _S(2048, "nextPageToken from the previous response", pattern=_TOKEN)
_SEND_UPDATES = {"type": "string", "enum": ["all", "externalOnly", "none"], "default": "none",
                 "description": "Who Google notifies by email; default none"}
_CAL = _S(254, "Calendar ID; default primary", pattern=_CAL_ID, default="primary")

_MAIL_PROPS = {
    "account": ACCOUNT,
    "to": _EMAILS("To recipients (bare addresses)"),
    "cc": _EMAILS("Cc recipients"),
    "bcc": _EMAILS("Bcc recipients"),
    "subject": _S(998, "Subject"),
    "body": _S(60_000, "Plain-text UTF-8 body"),
    "thread_id": _S(256, "Gmail threadId to reply within a thread", pattern=_ID),
    "in_reply_to": _S(998, "Message-ID of the replied message, with <>", pattern=r"<[^<>\s]{1,994}>"),
}
_EVENT_PROPS = {
    "summary": _S(1024, "Title"),
    "description": _S(8000, "Description"),
    "location": _S(1024, "Location"),
    "start": _WHEN, "end": _WHEN,
    "time_zone": _S(64, "IANA time zone, e.g. America/Lima", pattern=r"[A-Za-z][A-Za-z0-9_+/-]{0,63}"),
    "attendees": _EMAILS("Attendee emails (replaces the whole list on update)"),
    "send_updates": _SEND_UPDATES,
}


def _tool(name, description, props, required=()):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": props, "required": ["account", *required],
        "additionalProperties": False}}


SCHEMAS = {s["name"]: s for s in (
    _tool("gws_gmail_search", "Search Gmail messages with Gmail search syntax. Returns IDs; read each with gws_gmail_read.", {
        "account": ACCOUNT, "query": _S(2048, "Gmail query, e.g. 'from:a@b.com is:unread'"),
        "max_results": _I(1, 100, 10, "Max messages"), "page_token": _PAGE,
        "include_spam_trash": _B(False, "Include SPAM and Trash")}),
    _tool("gws_gmail_read", "Read one Gmail message by ID.", {
        "account": ACCOUNT, "message_id": _S(256, "Message ID", pattern=_ID),
        "format": {"type": "string", "enum": ["full", "metadata", "minimal", "raw"], "default": "full",
                   "description": "Native users.messages.get format"},
        "metadata_headers": {"type": "array", "maxItems": 20, "description": "Headers to return with format=metadata",
                             "items": {"type": "string", "maxLength": 76, "pattern": r"[!-9;-~]{1,76}"}}},
        ["message_id"]),
    _tool("gws_gmail_draft", "Create a plain-text Gmail draft. Sends nothing.", _MAIL_PROPS),
    _tool("gws_gmail_send", "Send a plain-text Gmail message. Irreversible.", _MAIL_PROPS, ["subject", "body"]),
    _tool("gws_drive_search", "Search Drive files with Drive q syntax. Check incompleteSearch when searching all drives.", {
        "account": ACCOUNT, "query": _S(2048, "Drive q query, e.g. \"name contains 'report' and trashed=false\""),
        "page_size": _I(1, 100, 20, "Max files"), "page_token": _PAGE,
        "include_shared_drives": _B(True, "Include shared drives")}),
    _tool("gws_drive_read", "Read a Drive file: metadata (JSON), content (alt=media) or export (Docs/Sheets/Slides to mime_type). content and export require output_path and write the bytes there.", {
        "account": ACCOUNT, "file_id": _S(256, "File ID", pattern=_ID),
        "mode": {"type": "string", "enum": ["metadata", "content", "export"], "default": "metadata",
                 "description": "What to read"},
        "mime_type": _S(192, "Export MIME type; required with mode=export", pattern=_MIME),
        "output_path": _S(1024, "Path relative to the profile's gws-oauth/files directory, no '..'; required with mode=content or export", pattern=_REL_PATH)},
        ["file_id"]),
    _tool("gws_drive_upload", "Upload a local file as a new Drive file. path is relative to the profile's gws-oauth/files directory.", {
        "account": ACCOUNT, "path": _S(1024, "Relative local path, no '..'", pattern=_REL_PATH),
        "name": _S(1024, "Drive name; default the local file name"),
        "parent_id": _S(256, "Destination folder ID", pattern=_ID),
        "mime_type": _S(192, "Content MIME type; default detected by gws", pattern=_MIME)},
        ["path"]),
    _tool("gws_drive_share", "Share a Drive file with a user, group or domain. For public links or ownership transfer use gws_api.", {
        "account": ACCOUNT, "file_id": _S(256, "File ID", pattern=_ID),
        "role": {"type": "string", "enum": ["reader", "commenter", "writer"], "description": "Role"},
        "type": {"type": "string", "enum": ["user", "group", "domain"], "description": "Grantee type"},
        "email_address": _S(254, "Email with type=user|group", pattern=_EMAIL),
        "domain": _S(253, "Domain with type=domain", pattern=r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"),
        "send_notification": _B(True, "Google sends a notification email (users/groups)"),
        "message": _S(4000, "Notification message")},
        ["file_id", "role", "type"]),
    _tool("gws_calendar_list", "List calendar events, expanding recurrences, ordered by start.", {
        "account": ACCOUNT, "calendar_id": _CAL,
        "time_min": _S(40, "Start, RFC3339 with zone"), "time_max": _S(40, "End, RFC3339 with zone"),
        "query": _S(1024, "Free-text search"), "max_results": _I(1, 250, 25, "Max events"),
        "page_token": _PAGE}),
    _tool("gws_calendar_freebusy", "Query free/busy for calendars in a time range.", {
        "account": ACCOUNT, "time_min": _S(40, "Start, RFC3339 with zone"), "time_max": _S(40, "End, RFC3339 with zone"),
        "calendar_ids": {"type": "array", "minItems": 1, "maxItems": 50, "default": ["primary"],
                         "items": {"type": "string", "maxLength": 254, "pattern": _CAL_ID},
                         "description": "Calendar IDs; default primary"},
        "time_zone": _EVENT_PROPS["time_zone"]},
        ["time_min", "time_max"]),
    _tool("gws_calendar_create", "Create an event. start and end must both be dates or both date-times.", {
        "account": ACCOUNT, "calendar_id": _CAL, **_EVENT_PROPS},
        ["summary", "start", "end"]),
    _tool("gws_calendar_update", "Patch an existing event; only the fields sent change.", {
        "account": ACCOUNT, "calendar_id": _CAL,
        "event_id": _S(1024, "Event ID", pattern=r"[A-Za-z0-9_@.-]{1,1024}"), **_EVENT_PROPS},
        ["event_id"]),
    _tool("gws_schema", "Show the native discovery schema of a gws method (service.resource.method) to build gws_api calls.", {
        "account": ACCOUNT,
        "method": _S(200, "E.g. gmail.users.messages.send", pattern=r"[a-z][A-Za-z0-9-]{0,40}(?:\.[A-Za-z][A-Za-z0-9]{0,60}){1,5}"),
        "resolve_refs": _B(False, "Expand $ref")},
        ["method"]),
)}


def _check(spec, value, where):
    kind = spec["type"]
    if kind == "string":
        if not isinstance(value, str):
            raise ValueError(f"{where} must be a string")
        if len(value) > spec.get("maxLength", 64):
            raise ValueError(f"{where} is too long")
        bad = _CTRL_MULTILINE if where in _MULTILINE else _CTRL
        if bad.search(value):
            raise ValueError(f"{where} contains control characters")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{where} must be one of {', '.join(spec['enum'])}")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            raise ValueError(f"{where} has an invalid format")
    elif kind == "integer":
        if type(value) is not int or not spec["minimum"] <= value <= spec["maximum"]:
            raise ValueError(f"{where} must be an integer {spec['minimum']}..{spec['maximum']}")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError(f"{where} must be a boolean")
    elif kind == "array":
        if not isinstance(value, list) or not spec.get("minItems", 0) <= len(value) <= spec["maxItems"]:
            raise ValueError(f"{where} must be a list of {spec.get('minItems', 0)}..{spec['maxItems']} items")
        for item in value:
            _check(spec["items"], item, where)


def validate(name, args):
    """Strictly validate ``args`` for tool ``name``; return a copy with defaults."""
    if name not in SCHEMAS:
        raise ValueError(f"unknown tool: {name}")
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    params = SCHEMAS[name]["parameters"]
    unknown = set(args) - set(params["properties"])
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(map(str, unknown)))}")
    missing = [key for key in params["required"] if key not in args]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    out = {}
    for key, spec in params["properties"].items():
        if key in args:
            _check(spec, args[key], key)
            out[key] = args[key]
        elif "default" in spec:
            out[key] = list(spec["default"]) if isinstance(spec["default"], list) else spec["default"]
    if out["account"] in (".", ".."):
        raise ValueError("account has an invalid format")
    return out


def _json(value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(text.encode("utf-8")) > MAX_ARG_BYTES:
        raise ValueError("request too large for one gws argument; use gws_api with a smaller payload")
    return text


def _call(path, params=None, body=None, *extra):
    argv = list(path)
    if params is not None:
        argv += ["--params", _json(params)]
    if body is not None:
        argv += ["--json", _json(body)]
    return argv + list(extra)


_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


def _instant(value, where):
    try:
        ok = bool(_RFC3339.fullmatch(value)) and datetime.fromisoformat(value) is not None
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{where} must be RFC3339 with seconds and a time zone (2026-01-02T10:00:00-05:00)")
    return value


def _when(value, where, zone):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{where} is not a valid date") from None
        return {"date": value}
    out = {"dateTime": _instant(value, where)}
    if zone:
        out["timeZone"] = zone
    return out


def _range(a):
    for key in ("time_min", "time_max"):
        if key in a:
            _instant(a[key], key)
    if "time_min" in a and "time_max" in a and \
            datetime.fromisoformat(a["time_min"]) >= datetime.fromisoformat(a["time_max"]):
        raise ValueError("time_min must be before time_max")


def _event(a, *, partial):
    zone = a.get("time_zone")
    body = {k: a[k] for k in ("summary", "description", "location") if k in a}
    for key in ("start", "end"):
        if key in a:
            body[key] = _when(a[key], key, zone)
    if "start" in body and "end" in body:
        s, e = body["start"], body["end"]
        if ("date" in s) != ("date" in e):
            raise ValueError("start and end must both be dates or both be date-times")
        if (s["date"] >= e["date"]) if "date" in s else \
                datetime.fromisoformat(s["dateTime"]) >= datetime.fromisoformat(e["dateTime"]):
            raise ValueError("end must be after start (all-day end date is exclusive)")
    if "attendees" in a:
        body["attendees"] = [{"email": e} for e in a["attendees"]]
    if partial and not body:
        raise ValueError("update needs at least one event field")
    if zone and "start" not in body and "end" not in body:
        raise ValueError("time_zone applies only together with start or end")
    return body


def _raw_mail(a, *, need_to):
    if need_to and not (a.get("to") or a.get("cc") or a.get("bcc")):
        raise ValueError("send needs at least one recipient in to, cc or bcc")
    msg = EmailMessage(policy=SMTP)
    for key, header in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
        if a.get(key):
            msg[header] = ", ".join(a[key])
    if "subject" in a:
        msg["Subject"] = a["subject"]  # EmailMessage also refuses CR/LF in headers.
    if "in_reply_to" in a:
        msg["In-Reply-To"] = a["in_reply_to"]
        msg["References"] = a["in_reply_to"]
    msg.set_content(a.get("body", ""))
    message = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}
    if "thread_id" in a:
        message["threadId"] = a["thread_id"]
    return message


def build_args(name, args):
    """Validated argv (without the ``gws`` binary) for typed tool ``name``."""
    a = validate(name, args)
    if name == "gws_gmail_search":
        params = {"userId": "me", "maxResults": a["max_results"],
                  "includeSpamTrash": a["include_spam_trash"]}
        params.update({k: a[s] for s, k in (("query", "q"), ("page_token", "pageToken")) if s in a})
        return _call(["gmail", "users", "messages", "list"], params)
    if name == "gws_gmail_read":
        params = {"userId": "me", "id": a["message_id"], "format": a["format"]}
        if "metadata_headers" in a:
            if a["format"] != "metadata":
                raise ValueError("metadata_headers requires format=metadata")
            params["metadataHeaders"] = a["metadata_headers"]
        return _call(["gmail", "users", "messages", "get"], params)
    if name == "gws_gmail_draft":
        return _call(["gmail", "users", "drafts", "create"], {"userId": "me"},
                     {"message": _raw_mail(a, need_to=False)})
    if name == "gws_gmail_send":
        return _call(["gmail", "users", "messages", "send"], {"userId": "me"}, _raw_mail(a, need_to=True))
    if name == "gws_drive_search":
        params = {"pageSize": a["page_size"],
                  "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size,webViewLink,parents)"}
        params.update({k: a[s] for s, k in (("query", "q"), ("page_token", "pageToken")) if s in a})
        if a["include_shared_drives"]:
            params.update(supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives",
                          fields="incompleteSearch," + params["fields"])
        return _call(["drive", "files", "list"], params)
    if name == "gws_drive_read":
        mode = a["mode"]
        if (mode == "export") != ("mime_type" in a):
            raise ValueError("mime_type is required with mode=export and only valid there")
        if (mode == "metadata") == ("output_path" in a):
            # Binary stdout through the text executor would corrupt content.
            raise ValueError("output_path is required with mode=content or export and only valid there")
        extra = ["--output", a["output_path"]] if "output_path" in a else []
        if mode == "export":
            return _call(["drive", "files", "export"], {"fileId": a["file_id"], "mimeType": a["mime_type"]}, None, *extra)
        params = {"fileId": a["file_id"], "supportsAllDrives": True}
        if mode == "content":
            params["alt"] = "media"
        else:
            params["fields"] = "id,name,mimeType,size,modifiedTime,createdTime,owners(emailAddress),parents,webViewLink,trashed"
        return _call(["drive", "files", "get"], params, None, *extra)
    if name == "gws_drive_upload":
        body = {"name": a.get("name") or a["path"].rstrip("/").rsplit("/", 1)[-1]}
        if "parent_id" in a:
            body["parents"] = [a["parent_id"]]
        extra = ["--upload", a["path"]]
        if "mime_type" in a:
            extra += ["--upload-content-type", a["mime_type"]]
        return _call(["drive", "files", "create"], {"supportsAllDrives": True, "fields": "id,name,mimeType,webViewLink"}, body, *extra)
    if name == "gws_drive_share":
        target = "domain" if a["type"] == "domain" else "email_address"
        other = "email_address" if target == "domain" else "domain"
        if target not in a or other in a:
            raise ValueError(f"type={a['type']} requires {target} and forbids {other}")
        body = {"role": a["role"], "type": a["type"],
                ("domain" if target == "domain" else "emailAddress"): a[target]}
        params = {"fileId": a["file_id"], "supportsAllDrives": True}
        if a["type"] != "domain":
            params["sendNotificationEmail"] = a["send_notification"]
        if "message" in a:
            if a["type"] == "domain" or not a["send_notification"]:
                raise ValueError("message requires a user/group share with send_notification=true")
            params["emailMessage"] = a["message"]
        return _call(["drive", "permissions", "create"], params, body)
    if name == "gws_calendar_list":
        _range(a)
        params = {"calendarId": a["calendar_id"], "maxResults": a["max_results"],
                  "singleEvents": True, "orderBy": "startTime"}
        params.update({k: a[s] for s, k in (("time_min", "timeMin"), ("time_max", "timeMax"),
                                             ("query", "q"), ("page_token", "pageToken")) if s in a})
        return _call(["calendar", "events", "list"], params)
    if name == "gws_calendar_freebusy":
        _range(a)
        body = {"timeMin": a["time_min"], "timeMax": a["time_max"],
                "items": [{"id": cid} for cid in a["calendar_ids"]]}
        if "time_zone" in a:
            body["timeZone"] = a["time_zone"]
        return _call(["calendar", "freebusy", "query"], None, body)
    if name == "gws_calendar_create":
        return _call(["calendar", "events", "insert"],
                     {"calendarId": a["calendar_id"], "sendUpdates": a["send_updates"]}, _event(a, partial=False))
    if name == "gws_calendar_update":
        return _call(["calendar", "events", "patch"],
                     {"calendarId": a["calendar_id"], "eventId": a["event_id"], "sendUpdates": a["send_updates"]},
                     _event(a, partial=True))
    # gws_schema
    return ["schema", a["method"], *(["--resolve-refs"] if a["resolve_refs"] else [])]
