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
                                    "reauth_required": False, "detail": "No se pudo comprobar esta cuenta."}
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
        schema={"name": "gws_login", "description": "Inicia OAuth de gws para una cuenta Google en el perfil actual desde cualquier plataforma, incluyendo CLI, TUI, Desktop, API server y gateway. Úsalo tras recibir el correo y los permisos deseados. Devuelve una URL de Google. Si el navegador corre en la misma máquina que Hermes, el callback llega solo al servidor local de gws, el navegador muestra 'Success' y la cuenta queda registrada sin pegar nada: confírmalo con gws_accounts o llamando gws_login de nuevo con el mismo account y sin callback_url, que devuelve el resultado guardado una sola vez (consumed=true). Si el navegador está en otro dispositivo, pide pegar en la misma sesión la URL localhost completa, con o sin http://; el gateway la intercepta automáticamente. Si llega al agente, completa con esta misma herramienta, el mismo account y callback_url; el callback pasa por el modelo y queda en el historial. Todo fallo trae un reason estable (intercambio: invalid_callback, expired, callback_rejected, gws_failed, identity_unconfirmed, identity_mismatch, account_exists, commit_failed, internal; previo: invalid_arguments, no_session_identity, client_missing, launch_failed, pending, no_pending). identity_mismatch incluye la identidad reportada enmascarada (a***o@dominio): casi siempre es un correo mal escrito en la solicitud; verifica la ortografía exacta antes de reintentar. Para una cuenta ya configurada, usa reauthorize=true: las credenciales anteriores se conservan hasta validar la nueva identidad y solo entonces se sustituyen. Nunca solicites passwords, tokens ni códigos sueltos.",
                "parameters": {"type": "object", "properties": {
                    "account": {"type": "string", "description": "Correo Google exacto a autorizar"},
                    "callback_url": {"type": "string", "description": "URL localhost completa pegada por el usuario para finalizar el login pendiente en esta misma sesión; disponible en cualquier plataforma"},
                    "scope_mode": {"type": "string", "enum": ["default", "readonly", "full", "custom"], "default": "full", "description": "Modo nativo de permisos de gws; full permite todos los servicios compatibles"},
                    "scopes": {"type": "array", "items": {"type": "string"}, "description": "Scopes OAuth exactos; requerido solo con scope_mode=custom"},
                    "reauthorize": {"type": "boolean", "default": False, "description": "Renueva una cuenta ya configurada. Es explícito: sin este flag se rechaza. Las credenciales anteriores siguen intactas hasta que la nueva identidad coincida y el commit tenga éxito; cancelar, vencer o rechazar la identidad las conserva; un fallo de reemplazo intenta restaurarlas."}},
                    "required": ["account"]}},
    )
    ctx.register_tool(
        name="gws_accounts", toolset="gws_oauth", handler=accounts,
        schema={"name": "gws_accounts", "description": "Lista las cuentas autorizadas en el perfil actual, sin revelar credenciales. Con check_health=true consulta gws auth status por cuenta y distingue credenciales guardadas de una sesión válida; gws sigue siendo el único responsable de refrescar tokens.",
                "parameters": {"type": "object", "properties": {
                    "check_health": {"type": "boolean", "default": False, "description": "Comprobación en vivo opcional (hasta 20 s por cuenta). Devuelve state=valid|invalid|stored|unreadable, stored y valid por separado, y reauth_required. No expone stdout/stderr ni tokens."}},
                    "required": []}},
    )
    ctx.register_tool(
        name="gws_api", toolset="gws_oauth", handler=api,
        schema={"name": "gws_api", "description": "Ejecuta gws tal cual para una cuenta explícita del perfil. Los argumentos se pasan sin limitar comandos, opciones ni acceso a archivos locales. Las respuestas exitosas mayores a 200000 caracteres devuelven output_file (archivo local UTF-8 completo) y output_bytes en vez de data; procesa ese archivo con código y elimínalo después. Para adjuntos Gmail decodifica el campo data del JSON como base64url. Si gws falla, devuelve error_code, category, action, reauth_required y exit_code con un mensaje saneado; nunca incluye stdout/stderr crudos.",
                "parameters": {"type": "object", "properties": {
                    "account": {"type": "string", "description": "Correo exacto de una cuenta autorizada"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Argumentos exactos posteriores al ejecutable gws, por ejemplo [\"drive\",\"files\",\"list\"]"}},
                    "required": ["account", "args"]}},
    )

    for service, module in (("gmail", watch_gmail), ("drive", watch_drive),
                            ("calendar", watch_calendar)):
        ctx.register_tool(name=module.TOOL_NAME, toolset="gws_oauth",
                          handler=watch_handler(service), schema=module.tool_schema())

    ctx.register_hook("pre_gateway_dispatch", watch_tools.bridge_callback(runtime.store, ctx))
