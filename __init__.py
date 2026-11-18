"""Hermes plugin: per-profile Google Workspace CLI accounts, OAuth and targeted watches."""
import json

from .oauth import LoginManager
from .router import account_health, bounded_gws, execute_gws, list_accounts


class _PluginClient:
    """Bound native gws transport for the three watch tools."""

    def __init__(self, home, gws_bin):
        self.root = home / "gws-oauth"
        self.gws_bin = gws_bin

    def call(self, account, args, params=None, body=None, timeout=30):
        argv = list(args)
        if params is not None:
            argv += ["--params", json.dumps(params, separators=(",", ":"), sort_keys=True)]
        if body is not None:
            argv += ["--json", json.dumps(body, separators=(",", ":"), sort_keys=True)]
        return bounded_gws(self.root, account, argv, gws_bin=self.gws_bin,
                           timeout=timeout, max_output=2_000_000)

    def scopes(self, account):
        result = bounded_gws(self.root, account, ["auth", "status"], gws_bin=self.gws_bin,
                             timeout=10, max_output=200_000)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return None
        scopes = result["data"].get("scopes")
        return [str(scope) for scope in scopes] if isinstance(scopes, list) else None

    def project(self, account):
        result = bounded_gws(self.root, account, ["auth", "status"], gws_bin=self.gws_bin,
                             timeout=10, max_output=200_000)
        if not result.get("ok") or not isinstance(result.get("data"), dict):
            return None
        project = result["data"].get("project_id") or result["data"].get("project")
        return str(project) if isinstance(project, str) and project else None


def register(ctx):
    from hermes_constants import get_hermes_home
    from gateway.session_context import get_session_env
    try:
        from gateway.session_context import bound_session_key
    except ImportError:  # older host/test double: no process-global fallback in production
        def bound_session_key():
            return get_session_env("HERMES_SESSION_KEY") or ""

    from . import watch as W
    from . import watch_calendar, watch_drive, watch_gmail, watch_routes, watch_tools

    install_root = get_hermes_home() / "gws-oauth"
    if install_root.is_symlink():
        raise RuntimeError(f"Refusing symlinked gws-oauth directory: {install_root}")
    install_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    install_root.chmod(0o700)

    manager = LoginManager()
    ctx.on_unload(manager.close)

    def root():
        return get_hermes_home() / "gws-oauth"

    def login(args, **kwargs):
        platform = get_session_env("HERMES_SESSION_PLATFORM") or get_session_env("HERMES_SESSION_SOURCE") or "cli"
        chat = get_session_env("HERMES_SESSION_CHAT_ID")
        user = get_session_env("HERMES_SESSION_USER_ID")
        profile = get_session_env("HERMES_SESSION_PROFILE")
        gateway_chat = bool(chat)
        if not gateway_chat:
            chat = kwargs.get("session_id") or get_session_env("HERMES_SESSION_ID")
            user = user or "local"
        if args.get("callback_url") is not None:
            return json.dumps(manager.finish(
                root(), platform, chat, user, args.get("account"), args["callback_url"],
                profile=profile,
            ))
        result = manager.start(
            root(), platform, chat, user, args.get("account"),
            ctx.get_config("gws_bin", default="gws"),
            (lambda callback: ctx.register_hook("pre_gateway_dispatch", callback)) if gateway_chat else None,
            profile=profile,
            scope_mode=args.get("scope_mode", "full"),
            scopes=args.get("scopes"),
            reauthorize=args.get("reauthorize", False),
        )
        return json.dumps(result)

    def accounts(args, **kwargs):
        names = list_accounts(root())
        payload = {"ok": True, "accounts": names}
        if args.get("check_health"):
            health = {}
            for name in names:
                try:
                    health[name] = account_health(
                        root(), name, gws_bin=ctx.get_config("gws_bin", default="gws"),
                    )
                except (OSError, ValueError):
                    health[name] = {"stored": True, "valid": None, "state": "stored",
                                    "reauth_required": False, "detail": "Could not check this account."}
            payload["health"] = health
        return json.dumps(payload)

    def api(args, **kwargs):
        try:
            result = execute_gws(
                root(), args.get("account"), args.get("args"),
                gws_bin=ctx.get_config("gws_bin", default="gws"),
            )
        except (OSError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result)

    class Runtime:
        def __init__(self):
            self.ctx = ctx
            self._store = None

        @property
        def store(self):
            if self._store is None:
                self._store = W.WatchStore(install_root.parent)
            return self._store

        @property
        def client(self):
            return _PluginClient(install_root.parent, ctx.get_config("gws_bin", default="gws"))

        @property
        def routes(self):
            return watch_routes.RouteManager(ctx)

        @property
        def config(self):
            keys = ("watch_gmail_topic", "watch_gmail_subscription", "watch_callback_url",
                    "watch_reconcile_seconds")
            return {key: ctx.get_config(key, default="") for key in keys}

        def origin(self):
            if get_hermes_home().resolve() != install_root.parent.resolve():
                raise W.WatchError("profile_mismatch", "Watch runtime belongs to another profile")
            profile = get_session_env("HERMES_SESSION_PROFILE") or getattr(ctx, "profile_name", None) or "default"
            return W.origin_from_session(
                env=get_session_env,
                session_key=bound_session_key(),
                session_id=get_session_env("HERMES_SESSION_ID"),
                profile=profile,
            )

    runtime = Runtime()

    from .workspace_tools import SCHEMAS, build_args
    from .api_policy import make_pre_tool_call_hook

    ctx.register_hook("pre_tool_call", make_pre_tool_call_hook(build_args, SCHEMAS))

    def workspace_handler(name):
        def handler(args, **kwargs):
            try:
                argv = build_args(name, args)
                result = execute_gws(
                    root(), args.get("account"), argv,
                    gws_bin=ctx.get_config("gws_bin", default="gws"),
                )
            except ValueError as exc:
                result = {"ok": False, "error_code": "invalid_arguments", "error": str(exc)}
            except OSError:
                result = {"ok": False, "error_code": "local_error",
                          "error": "The local Workspace command could not run."}
            return json.dumps(result)
        return handler

    for name, schema in SCHEMAS.items():
        ctx.register_tool(name=name, toolset="gws_oauth",
                          handler=workspace_handler(name), schema=schema)

    def watch_handler(service):
        def handler(args, **kwargs):
            try:
                return json.dumps(watch_tools.handle(service, args, runtime))
            except Exception:
                return json.dumps({"ok": False, "error_code": "internal_error",
                                   "error": "The watch tool failed before producing a safe result."})
        return handler

    ctx.register_tool(
        name="gws_login", toolset="gws_oauth", handler=login,
        schema={"name": "gws_login", "description": "Authorize one Google account for this single-operator Hermes profile on any frontend. Returns a Google consent URL; full native scopes remain the default. A browser on the Hermes host completes automatically: confirm with gws_accounts or repeat gws_login without callback_url for the one-shot stored outcome. From another device, paste the complete localhost callback URL in the originating session; gateway ingress intercepts it, otherwise finish using callback_url and the same account. Explicit callbacks pass through the model/history. Use reauthorize=true only to replace an existing account; old credentials remain until the new identity matches. Failures have stable reason codes; identity_mismatch reports a masked identity, so verify the exact requested spelling. Never request passwords, exported credentials, refresh tokens or standalone codes.",
                "parameters": {"type": "object", "properties": {
                    "account": {"type": "string", "description": "Exact Google email address to authorize"},
                    "callback_url": {"type": "string", "description": "Complete localhost callback URL pasted by the user to finish this session’s pending login on any frontend"},
                    "scope_mode": {"type": "string", "enum": ["default", "readonly", "full", "custom"], "default": "full", "description": "Native gws scope preset; full requests all supported service scopes"},
                    "scopes": {"type": "array", "items": {"type": "string"}, "description": "Exact OAuth scopes, required only with scope_mode=custom"},
                    "reauthorize": {"type": "boolean", "default": False, "description": "Explicitly renew an existing account. Old credentials remain until identity validation and replacement succeed; cancellation, expiry or rejection preserves them, and replacement failure attempts rollback."}},
                    "required": ["account"]}},
    )
    ctx.register_tool(
        name="gws_accounts", toolset="gws_oauth", handler=accounts,
        schema={"name": "gws_accounts", "description": "List this operator’s Google accounts in the active profile without credentials. check_health=true obtains native status and a sanitized granted-scope summary; stored files are distinguished from confirmed validity. Native gws handles refresh.",
                "parameters": {"type": "object", "properties": {
                    "check_health": {"type": "boolean", "default": False, "description": "Optional native health check (up to 20 seconds per account). Returns state=valid|invalid|stored|unreadable, stored, valid, reauth_required and safe scope metadata, never raw output or tokens."}},
                    "required": []}},
    )
    ctx.register_tool(
        name="gws_api", toolset="gws_oauth", handler=api,
        schema={"name": "gws_api", "description": "Advanced native Workspace API escape hatch for one explicit account. Prefer curated tools and gws_schema for common tasks. Native auth lifecycle/export is blocked; mutations, destructive operations, sends/shares and local file input/output require Hermes approval. Pass CLI tokens, not a shell command. Successful output above 200000 characters returns output_file/output_bytes: process locally and remove after use. Gmail attachment data is base64url. Native failures return sanitized error_code/category/action/reauth_required/exit_code, never raw failure output.",
                "parameters": {"type": "object", "properties": {
                    "account": {"type": "string", "description": "Exact email address of an authorized account"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Exact tokens after the gws executable, for example [\"drive\",\"files\",\"list\"]"}},
                    "required": ["account", "args"]}},
    )

    for service, module in (("gmail", watch_gmail), ("drive", watch_drive),
                            ("calendar", watch_calendar)):
        ctx.register_tool(name=module.TOOL_NAME, toolset="gws_oauth",
                          handler=watch_handler(service), schema=module.tool_schema())

    ctx.register_hook("pre_gateway_dispatch", watch_tools.bridge_callback(runtime.store, ctx))
