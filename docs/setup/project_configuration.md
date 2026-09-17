# Project Configuration (`shikshalokam_mohini/`)

This is the Django project package — settings, URL root, ASGI/WSGI entry
points, and Celery app definition — as opposed to `chatbot/`, which is the
single Django app holding all business logic. `DJANGO_SETTINGS_MODULE` is
`shikshalokam_mohini.settings` everywhere (`manage.py`, `celery_config.py`,
`asgi.py`, `wsgi.py`).

The package name predates the current scope of the project (it was originally
the Mohini/Shikshalokam project); it has no relation to the now-deleted
`shikshalokam` Django app (see the repo-root `CODE_CLEANUP_PLAN.md`) beyond
sharing part of a name.

## Files

| File | Purpose |
|---|---|
| `settings.py` | All Django settings — see below. |
| `urls.py` | Root URL configuration. |
| `asgi.py` | ASGI entry point; composes HTTP and WebSocket protocol routing. |
| `wsgi.py` | Plain WSGI entry point (used only where ASGI is not available). |
| `celery_config.py` | Celery app definition and task autodiscovery. |
| `middleware.py` | One project-level custom middleware, `StaffRequiredForLogViewer`. |

## `settings.py`

- **Environment loading.** `load_dotenv()` reads `.env` from the working
  directory. A separate `load_secrets()` function looks for
  `config/secrets.json` at a hardcoded production path
  (`/home/ubuntu/saathi-backend/config/secrets.json`), then
  `<repo>/config/secrets.json`, then `<cwd>/config/secrets.json`, first match
  wins — `config/secrets.json` itself is untracked (see `.gitignore`); the
  repo only ships `config/__init__.py`.
- **`INSTALLED_APPS`** — notably: `chatbot` is the only project-specific app
  left (`shikshalokam` and `observability` were removed; see
  [Chatbot Overview](../apps/chatbot/overview.md)); `jazzmin` provides the
  admin theme; `simple_history` backs every model's audit trail
  (`HistoricalRecords()`); `django_s3_storage`/`storages` back file storage;
  `import_export` backs the CSV/XLSX import-export admin mixins; `log_viewer`
  serves the `/log-viewer/` pages; `django_crontab` is installed but
  `CRONJOBS = []` — no cron jobs are currently registered through it.
- **`MIDDLEWARE`** — standard Django stack plus, in order after the built-ins:
  `chatbot.middlewares.VerifyAuthToken` (see
  [Authentication](../apps/chatbot/chatbot_auth.md)) and
  `shikshalokam_mohini.middleware.StaffRequiredForLogViewer` (restricts
  `/log-viewer/` to authenticated staff users, redirecting anonymous users to
  `/admin/login/` and returning `403` for authenticated non-staff users). In
  `DEBUG` mode, `querycount.middleware.QueryCountMiddleware` is appended for
  per-request SQL query-count reporting.
- **Database** — PostgreSQL only, all connection parameters from environment
  variables, including optional SSL (`PG_SSL_MODE`, `PG_SSL_ROOT_CERT`).
- **Channels/Redis** — `CHANNEL_LAYERS` uses `channels_redis.core.RedisChannelLayer`
  against `REDIS_URL` (built from `REDIS_HOST`/`REDIS_PORT`/`REDIS_USE_SSL`),
  with an elevated `channel_capacity` for `websocket.send!*` (100,000) — sized
  for the volume of per-token streaming chunks sent during LLM streaming (see
  [WebSocket Response Handling](../apps/chatbot/chatbot_response_handlers.md)
  §5.5). `CACHES['default']` uses `django_redis` against Redis DB 1
  (channels use the default DB), with a pickle serializer and zlib
  compression.
- **File storage** — `STORAGE_CLOUD_PROVIDER` (env, default `AWS`) selects one
  of four backends defined in `STORAGE_BACKENDS`: `AWS` (S3), `GCP`, `AZURE`,
  or `LOCAL` (filesystem, for local development). An unsupported value raises
  at import time rather than falling back silently. This setting is
  independent of, but conceptually related to, the storage backend selection
  in `chatbot/services/storage/` (see [Utils](../apps/chatbot/chatbot_utils.md));
  the `STORAGES`/`MEDIA_ROOT`/`MEDIA_URL` Django-level settings configured
  here back Django's own `FileField`/`ImageField` uploads, while
  `services/storage/` is a separate abstraction used specifically for
  generated PDF/DOCX documents.
- **Auth** — `SIMPLE_JWT` configures 7-day access/sliding-refresh token
  lifetimes, HS256, and a custom `TOKEN_SERIALIZER`
  (`chatbot.serializers.ProfileTokenObtainPairSerializer` — note this appears
  to reference a `chatbot.serializers` module rather than the actual
  `chatbot.serializer` package; verify this path is current if JWT token
  claims ever need to be debugged). `SESSION_COOKIE_HTTPONLY = False` and
  `SESSION_COOKIE_SAMESITE = None` are both relaxed from Django's secure
  defaults — the frontend is a separate origin that needs to read/send the
  session cookie cross-site.
- **CORS/CSRF** — `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` are both
  environment-driven (the latter defaults to a wildcarded list of
  `shikshalokam.org`/`gritworks.ai`/`localhost:3000` if unset).
- **Sentry** — initialized unconditionally at import time
  (`sentry_sdk.init(dsn=os.getenv('SENTRY_DSN'), ...)`) with 100% trace and
  profile sampling; an empty/unset `SENTRY_DSN` effectively disables
  reporting without needing a separate feature flag.
- **Logging** — four `TimedRotatingFileHandler`s (`debug.log`, `info.log`, and
  two both pointed at `error.log` for `WARNING` and `ERROR` levels) under
  `LOGGING_DIR` (`<repo>/logs/`), rotated daily with 30 backups kept. The
  `django` logger (used as `logging.getLogger('django')` throughout
  `chatbot/`) is the only logger explicitly configured, at
  `DEFAULT_LOG_LEVEL` (env, default `INFO`) — since `WARNING`-level records
  are routed only into `error.log` and are otherwise not surfaced anywhere
  distinctly from errors, application code in this repo is written to use
  `logger.info`/`logger.error` rather than `logger.warning`.
- **`LOG_VIEWER_*`** settings back the `/log-viewer/` admin pages (via the
  `log_viewer` app), restricted by `StaffRequiredForLogViewer` above.

## `urls.py`

Root URL configuration, mounted before the app-specific
[`chatbot/urls.py`](../apps/chatbot/chatbot_urls.md):

```python
urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health_views.health_check, name='health_check'),
    path('docs/', include_docs_urls(title='API Documentation')),
    re_path(r'^api/storage/upload-local/(?P<object_key>.+)$', aws_views.upload_media_local, name='upload_media_local'),
    path("", include("chatbot.urls", namespace="chatbot")),
    path('log-viewer/', include('log_viewer.urls')),
]
```

`admin.site.site_header` is overridden to `'Mohini Admin Panel'` here (Jazzmin
theme labels are set separately, in `settings.JAZZMIN_SETTINGS`). In `DEBUG`
mode, `static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)` is
appended so locally-stored media files are servable without a separate web
server.

## `asgi.py` — protocol routing

```python
application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(
            CookieMiddleware(SessionMiddleware(URLRouter(chatbot.routing.websocket_urlpatterns)))
        )
    ),
})
```

`django.setup()` is called explicitly a second time (after
`get_asgi_application()` already triggers it) purely as defensive insurance
before `chatbot.routing` — which imports models — is imported. WebSocket
connections pass through, in order: origin validation against
`ALLOWED_HOSTS`, Channels' session-based auth middleware stack, cookie
middleware, session middleware, then the app's own
`websocket_urlpatterns` (see [Consumers](../apps/chatbot/chatbot_consumers.md)
and [WebSocket Response Handling](../apps/chatbot/chatbot_response_handlers.md)
— note the WebSocket authentication actually used to identify the caller is a
separate, app-level `authenticate` message handled inside
`AsyncSocketConsumer`, layered on top of, not replacing, this
Channels-session-based stack).

The production ASGI server is Uvicorn (see `Dockerfile`:
`uvicorn shikshalokam_mohini.asgi:application ... --ws-ping-interval 30 --ws-ping-timeout 600`),
not Daphne, despite `daphne` being listed in `INSTALLED_APPS` (needed there
only so Django recognizes ASGI-specific management commands/checks).

## `celery_config.py`

```python
app = Celery(
    'shikshalokam_mohini',
    backend=f'redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}',
    broker=f'redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}',
)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks([
    'chatbot.celery_tasks.common_chat_tasks',
    'chatbot.celery_tasks.flow_tasks',
    'chatbot.celery_tasks.title_tasks',
])
```

Redis DB index defaults to `0` here (`REDIS_DB` env var) — distinct from the
Channels layer, which always uses the default DB, and the Django cache, which
is pinned to DB `1` in `settings.py`. Autodiscovery is scoped to exactly the
three modules listed, not the whole `chatbot.celery_tasks` package — a new
task module must be added to this list explicitly to be picked up by a
worker; see [Celery Tasks](../apps/chatbot/chatbot_celery_tasks.md) for what
each of the three currently contains.
