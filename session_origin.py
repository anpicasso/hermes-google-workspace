"""Automatic watch origins from bound runtime context, never process-global routes.

Compatibility exception: the bound-only reader and delegation guard are internal
Hermes APIs. Keep this adapter small and fail closed if their contract disappears.
"""
from __future__ import annotations

if __package__:
    from .watch import WatchError
else:
    from watch import WatchError


def _text(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise WatchError("no_trusted_origin", "Trusted runtime session context is missing or invalid")
    return value


def current_origin(session_id, profile, *, home=None):
    """Snapshot this tool call's origin; no manual bind or model destination.

    A child's physical ID can be rebound while retaining its parent's route.
    Check delegation lineage first, not just equality of the physical IDs.
    """
    try:
        from agent.delegation_context import is_delegated_child_process_context
        from gateway.session_context import _bound_session_value, _SESSION_TURN_EVENT
        from gateway.session_identity import identity_of
    except ImportError:
        raise WatchError("no_trusted_origin", "This Hermes host lacks the required bound-context adapter") from None
    if is_delegated_child_process_context():
        raise WatchError("no_trusted_origin", "Start or manage watches from the originating agent, not a delegated child")

    sid, profile = _text(session_id), _text(profile)
    # ponytail: read only bound ContextVars; get_session_env can borrow global routes.
    def bound(field, *, optional=False):
        return _text(_bound_session_value("HERMES_SESSION_" + field), optional=optional)

    session_profile = _bound_session_value("HERMES_SESSION_PROFILE")
    if session_profile == "":
        # The primary gateway route has no wire profile. Only its pinned turn
        # identity can supply one; an unset ContextVar is not a primary route.
        identity = identity_of(getattr(_SESSION_TURN_EVENT.get(), "source", None))
        if identity is None or identity.runtime_home.resolve() != home:
            raise WatchError("no_trusted_origin", "No matching gateway profile identity is bound to this turn")
        session_profile = identity.runtime_profile
    if bound("ID") != sid or _text(session_profile) != profile:
        raise WatchError("no_trusted_origin", "Tool session or profile does not match its runtime origin")
    platform = bound("PLATFORM")
    if platform in {"webhook", "msgraph_webhook", "api", "api_server", "cron",
                    "cli", "tui", "local", "desktop", "kanban", "tool", "gateway"}:
        raise WatchError("no_trusted_origin", "This surface cannot originate conversational watch delivery")
    if _bound_session_value("HERMES_CRON_SESSION") == "1":
        raise WatchError("no_trusted_origin", "Scheduled runs cannot borrow a conversation origin")
    actor = bound("ACTOR_ID")  # Never fall back to route user_id after actor clearing.
    origin = {"profile": profile, "principal": actor, "user_id": actor,
              "platform": platform, "chat_id": bound("CHAT_ID"),
              "session_id": sid, "session_key": bound("KEY")}
    for field in ("thread_id", "scope_id", "parent_chat_id", "chat_type"):
        origin[field] = bound(field.upper(), optional=True)
    return origin
