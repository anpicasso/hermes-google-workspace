"""Explicit command-context bindings; never infer identity from environment variables."""
from __future__ import annotations

import threading
import time

if __package__:
    from .watch import WatchError
else:
    from watch import WatchError


def _text(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise WatchError("no_trusted_origin", "Trusted command context is missing or invalid")
    return value


class OriginBindings:
    """Bounded process-local bindings. Restart, expiry and /new require /gws-bind again."""

    def __init__(self, *, ttl=3600, max_entries=1024, clock=time.monotonic):
        if not 0 < ttl <= 86400 or not 1 <= max_entries <= 10000:
            raise ValueError("Binding limits must be positive and bounded")
        self.ttl, self.max_entries, self.clock = ttl, max_entries, clock
        self._bindings = {}
        # ponytail: one short lock; no network or host calls under it.
        self._lock = threading.Lock()

    def _cleanup(self, now):
        for key, (_, expires) in list(self._bindings.items()):
            if expires <= now:
                del self._bindings[key]

    async def bind(self, raw_args, *, source=None, session_id="", session_key="",
                   platform="", profile):
        """Register as an async slash command, with profile fixed by the plugin instance."""
        if raw_args and str(raw_args).strip():
            return "Usage: /gws-bind (no arguments; destination comes from trusted command context)."
        try:
            sid, key, profile = _text(session_id), _text(session_key), _text(profile)
            platform = _text(getattr(platform, "value", platform))
            if platform in ("webhook", "api", "api_server", "cron"):
                raise WatchError("no_trusted_origin", "This surface cannot bind watch delivery")
            source_platform = getattr(source, "platform", None)
            if getattr(source_platform, "value", source_platform) != platform:
                raise WatchError("no_trusted_origin", "Command platform does not match its source")
            source_profile = getattr(source, "profile", None)
            if source_profile not in (None, "", profile):
                raise WatchError("no_trusted_origin", "Command source belongs to another profile")
            actor = _text(getattr(source, "user_id", None))
            origin = {"profile": profile, "principal": actor, "user_id": actor,
                      "platform": platform, "chat_id": _text(getattr(source, "chat_id", None)),
                      "session_id": sid, "session_key": key}
            for field in ("thread_id", "scope_id", "parent_chat_id", "chat_type"):
                origin[field] = _text(getattr(source, field, None), optional=True)
            with self._lock:
                now = self.clock()
                self._cleanup(now)
                previous = self._bindings.get(sid)
                if previous and previous[0] != origin:
                    raise WatchError("origin_conflict", "Physical session is already bound to another origin")
                if not previous and len(self._bindings) >= self.max_entries:
                    raise WatchError("origin_capacity", "Binding capacity reached; wait for bindings to expire")
                self._bindings[sid] = (origin, now + self.ttl)
            return "GWS origin bound to this physical session. Run /gws-bind again after /new or expiry."
        except WatchError as exc:
            return "GWS binding refused: " + exc.message

    def origin(self, session_id, profile):
        sid, profile = _text(session_id), _text(profile)
        with self._lock:
            self._cleanup(self.clock())
            bound = self._bindings.get(sid)
            if bound is None or bound[0]["profile"] != profile:
                raise WatchError("no_trusted_origin", "Run /gws-bind in this session first; child agents are not automatically bound")
            return dict(bound[0])
