"""Consent policy for model-driven gws calls, enforced by Hermes ``pre_tool_call``.

Reads and help/schema run directly. Every other operation (writes, deletes,
sends, sharing, uploads, local output files, request bodies, dry runs, unknown
helpers/methods or options this parser does not understand) is escalated to
the native Hermes human-approval gate. The ``auth`` namespace is always
blocked: it can print decrypted credentials. Approval is never taken from tool
arguments; only the host gate decides.
"""
from __future__ import annotations

import json

# Native gws method flags (``gws <svc> <res> <method> --help``).
_VALUE_FLAGS = {"--params", "--json", "--upload", "--upload-content-type", "--output", "-o",
                "--format", "--api-version", "--page-limit", "--page-delay", "--sanitize"}
_BOOL_FLAGS = {"--dry-run", "--page-all", "--help", "-h", "--resolve-refs"}
_GATED_FLAGS = {"--json", "--upload", "--output", "-o", "--dry-run"}
# ponytail: method-name allowlist; anything new or unusual asks the human.
_READ_METHODS = {"get", "list", "search", "query", "batchGet", "getProfile"}
_READ_BODY_METHODS = {"calendar.freebusy.query"}  # POST, but read-only
_READ_HELPERS = {"gmail.+triage", "gmail.+read", "sheets.+read", "calendar.+agenda",
                 "workflow.+standup-report", "workflow.+meeting-prep", "workflow.+weekly-digest"}

ALLOW, APPROVE, BLOCK = "allow", "approve", "block"


def parse(argv):
    """Split argv into positionals, known flags and unknown option names."""
    positionals, flags, unknown = [], {}, []
    i = 0
    while i < len(argv):
        token = argv[i]
        i += 1
        if not token.startswith("-") or token == "-":
            positionals.append(token)
            continue
        name, has_value, value = token.partition("=")
        if name in _VALUE_FLAGS:
            if not has_value:
                if i >= len(argv):  # dangling flag: let gws reject it, but not unreviewed
                    unknown.append(name)
                    continue
                value = argv[i]
                i += 1
            flags[name] = value
        elif name in _BOOL_FLAGS and not has_value:
            flags[name] = True
        else:
            unknown.append(name)
    return positionals, flags, unknown


def classify(argv):
    """Return ``(decision, operation)`` for one gws argv."""
    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        return BLOCK, "invalid"
    positionals, flags, unknown = parse(argv)
    if "auth" in (p.lower() for p in positionals[:2]):  # also `gws help auth ...`
        return BLOCK, "auth"
    if "--help" in flags or "-h" in flags:
        return ALLOW, "help"
    if not positionals:
        return APPROVE, "unknown"
    gated = bool(_GATED_FLAGS & flags.keys())
    if positionals[0] == "schema":
        return (APPROVE if unknown or gated else ALLOW), "schema"
    if len(positionals) > 1 and positionals[1].startswith("+"):
        # Unknown helper flags also need review; a future flag may add side effects.
        op = f"{positionals[0]}.{positionals[1]}"
        return (ALLOW if op in _READ_HELPERS and not gated and not unknown else APPROVE), op
    op = ".".join(positionals)
    if op in _READ_BODY_METHODS:
        gated = bool((_GATED_FLAGS - {"--json"}) & flags.keys())
    if unknown or gated or len(positionals) < 3 or positionals[-1] not in _READ_METHODS:
        return APPROVE, op
    return ALLOW, op


_LONG_TEXT = ("body", "description", "message")


def _typed_preview(args, limit=300):
    """Recipients, targets and paths always shown in full; only free text is shortened."""
    shown = {k: v for k, v in args.items() if k not in _LONG_TEXT}
    for key in _LONG_TEXT:
        if key in args:
            text = str(args[key])
            shown[key] = text if len(text) <= limit else f"{text[:limit]}… ({len(text)} chars)"
    return json.dumps(shown, ensure_ascii=False)


def _argv_preview(argv, limit=1500):
    text = json.dumps(argv, ensure_ascii=False)
    return text if len(text) <= limit else f"{text[:limit]}… (truncated, {len(text)} chars)"


def make_pre_tool_call_hook(build_args=None, typed_names=()):
    """Hermes ``pre_tool_call`` callback for ``gws_api`` and typed tools.

    ``build_args(tool_name, args)`` must return the gws argv a typed tool runs.
    """
    typed = frozenset(typed_names)

    def hook(tool_name="", args=None, **_):
        args = args if isinstance(args, dict) else {}
        if tool_name == "gws_api":
            argv = args.get("args")
        elif tool_name in typed and build_args is not None:
            try:
                argv = build_args(tool_name, args)
            except Exception:
                return {"action": "block", "message": f"{tool_name}: invalid arguments; nothing was run."}
        else:
            return None
        decision, op = classify(argv)
        if decision == ALLOW:
            return None
        if decision == BLOCK:
            return {"action": "block", "message": (
                "gws auth commands are not available to the agent (credentials stay private); "
                "use gws_accounts check_health or operator-side native gws auth." if op == "auth"
                else "gws args must be a list of strings.")}
        account = str(args.get("account") or "?")[:128]
        preview = _typed_preview(args) if tool_name in typed else _argv_preview(argv)
        return {"action": "approve", "rule_key": f"gws:{account}:{op}"[:300],
                "message": f"Google Workspace {op} as {account}: gws {preview}"}

    return hook


if __name__ == "__main__":
    assert classify(["drive", "files", "list"]) == (ALLOW, "drive.files.list")
    assert classify(["drive", "files", "delete", "--params", '{"q":"list"}'])[0] == APPROVE
    assert classify(["--format", "json", "auth", "export"])[0] == BLOCK
    print("ok")
