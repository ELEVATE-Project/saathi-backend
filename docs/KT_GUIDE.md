# Backend Knowledge Transfer Guide

## Purpose

This page is the single index of every module in the Saathi backend for the
purpose of structuring knowledge-transfer (KT) sessions and onboarding. For
each module it records: where the code lives, where it is documented, how
deep that documentation currently goes, and what — if anything — is still
missing. It is a planning and tracking document, not a substitute for the
module-level pages it links to.

This guide reflects the documentation state as of the current cleanup pass
(see the repo-root `CODE_CLEANUP_PLAN.md` for the full history of what was
removed from the codebase and why). It should be revisited whenever a module
is added, removed, or substantially changed, since none of the reference
documentation it links to is auto-generated — everything listed below can
drift out of sync with the code again if it is not updated alongside future
changes.

## System overview

The backend is a single Django project, `shikshalokam_mohini` (project
configuration and settings root — see
[Project Configuration](setup/project_configuration.md)), running one
Django app, `chatbot` (all business logic — see
[Chatbot Overview](apps/chatbot/overview.md)). Two Django apps that
previously existed in this repo — `shikshalokam` and `observability` — were
removed in full during the cleanup effort; only stale, git-ignored
`__pycache__` directories remain on disk for them, with no importable source.

The center of the system is a single WebSocket route (`ws/common/`) through
which every chat interaction, regardless of bot type, flows. A message
received on that socket is authenticated, persisted, and handed to a Celery
worker, which runs it through an orchestrator → strategy → response-handler
pipeline that calls an external LLM gateway (with an in-process tool-
execution loop for knowledge-base search and other tool calls) and delivers
the result back to the original socket via the Django Channels channel layer.
This pipeline — the most complex and most frequently touched part of the
codebase — is documented in full depth in
[WebSocket Response Handling](apps/chatbot/chatbot_response_handlers.md); a
new engineer should treat that page, together with
[Consumers](apps/chatbot/chatbot_consumers.md),
[Services](apps/chatbot/chatbot_services.md), and
[Strategies](apps/chatbot/chatbot_strategies.md), as the highest-priority
reading in this guide.

## Module checklist

Coverage key: **Full** — accurate and reasonably complete as of this pass;
**Partial** — documented, but narrower than the module itself, or documented
only indirectly through another page; **Gap** — not documented anywhere.

### 1. Foundations

| Module | Source | Documentation | Coverage |
|---|---|---|---|
| Project configuration (settings, URLs root, ASGI/WSGI, Celery app) | `shikshalokam_mohini/` | [Project Configuration](setup/project_configuration.md) | Full |
| Developer environment setup | — | [Developer Setup](setup/developer_setup.md) | Full |
| Domain glossary | repo root `UBIQUITOUS_LANGUAGE.md` | (root file, not under `docs/`) | Full |
| Enum/choice fields | `chatbot/models/enums.py` | [Enums](backend/enums.md) | Full |
| Django models | `chatbot/models/` | [Models](apps/chatbot/models.md) | Full — backfilled this pass to cover every current model (`CompanyChatFeedback`, `Flow`, `ImageConfiguration`, `Language`, `LanguageProviderConfig`, `MediaTemplate`, `PDFTemplates`, `Provider`), and corrected several existing entries that had drifted (`BotVernacular`, `ChatSession`, `CompanyBot`, `CompanyStateMachine`, `Voice`). No regeneration script exists any more (`generate_models_docs.py` was removed as unused) — this file must be hand-updated alongside future model changes. |
| Authentication | `chatbot/auth.py`, `chatbot/middlewares/` | [Authentication](apps/chatbot/chatbot_auth.md) | Full |

### 2. Request/response surface

| Module | Source | Documentation | Coverage |
|---|---|---|---|
| URL routing (HTTP + WebSocket) | `chatbot/urls.py`, `chatbot/routing.py` | [URLs and Routing](apps/chatbot/chatbot_urls.md) | Full |
| API and admin views | `chatbot/views/` | [Views](apps/chatbot/views.md) | Full |
| Django admin | `chatbot/admin/` | [Admin](apps/chatbot/chatbot_admin.md) | Full |
| DRF serializers and filters | `chatbot/serializer/`, `chatbot/filter/` | [Serializer and Filters](apps/chatbot/chatbot_serializer_and_filter.md) | Full |

### 3. Real-time chat pipeline (highest priority — read in this order)

| Module | Source | Documentation | Coverage |
|---|---|---|---|
| WebSocket consumers (transport layer) | `chatbot/consumers/` | [Consumers](apps/chatbot/chatbot_consumers.md) | Full |
| Core orchestration services | `chatbot/services/core/` | [Services](apps/chatbot/chatbot_services.md) | Full |
| Bot strategies | `chatbot/services/strategies/` | [Strategies](apps/chatbot/chatbot_strategies.md) | Full |
| Response handlers (LLM gateway call, tool loop, response processing) | `chatbot/services/response_handlers/` | [WebSocket Response Handling](apps/chatbot/chatbot_response_handlers.md) | **Deep** — dedicated line-by-line reference covering `BaseResponseHandler` and `CommonResponseHandler` end to end, including the tool-execution loop, streaming vs. non-streaming call paths, cross-turn state kept on `ChatSession.other_params`, and every free-flow tool (`download_file`, `submit_user_context`, `process_user_input`/`respond_to_user`). |
| Preprocessing / postprocessing hooks | `chatbot/services/preprocessing/`, `chatbot/services/postprocessing/` | [WebSocket Response Handling §7](apps/chatbot/chatbot_response_handlers.md) | Full |
| Async background tasks (message translation/delivery, flow dispatch, title generation) | `chatbot/celery_tasks/` | [Celery Tasks](apps/chatbot/chatbot_celery_tasks.md) | Full |

### 4. LLM and language

| Module | Source | Documentation | Coverage |
|---|---|---|---|
| LLM gateway + direct provider calls | `chatbot/llm_models/` | [LLM Integration](backend/llm.md) | Full — corrected this pass to describe the actual current split between the gateway path (`llm_gateway.py`, used for every main chat turn) and the legacy direct-provider path (`llm_script.py`, used only by preprocessing/postprocessing); the previous version of this page only described the legacy path and did not mention the gateway at all. |
| Translation/transliteration providers | `chatbot/translate/` | [Translation Layer](apps/chatbot/chatbot_translate.md), [Translate](backend/translate.md) | Full — this pass added two previously-undocumented providers (`translate/custom/custom_llm.py`, `translate/google/google_glossary.py`) to `backend/translate.md`. |
| Knowledge-base vector search | `chatbot/services/vector/vector_service.py` | [Qdrant integration docs](integrations/vector_db/qdrant/developer_guide.md), plus [WebSocket Response Handling §5.3](apps/chatbot/chatbot_response_handlers.md) for how it is invoked as a tool | Partial — covered from the integration/infrastructure side and from the calling side, but the service module itself has no dedicated backend-app page. |

### 5. Documents, storage and media

| Module | Source | Documentation | Coverage |
|---|---|---|---|
| Document templates and PDF/DOCX generation | `chatbot/utils/media_preview/`, `MediaTemplate` model | [Utils](apps/chatbot/chatbot_utils.md), [Admin](apps/chatbot/chatbot_admin.md) | Full |
| Generated-document storage backends | `chatbot/services/storage/` (`base_storage_handler.py`, `local_storage_handler.py`, `aws_storage_handler.py`, `storage_factory.py`) | — | **Gap.** No page describes this factory/backend pair at all, despite being the module `media_creation.py` (documented in [Utils](apps/chatbot/chatbot_utils.md)) hands generated PDFs/DOCX files to for persistence. Needs its own short page or a new section in `chatbot_utils.md`. |
| PDF rendering server setup | Gotenberg (external service) | [Gotenberg Server Setup](setup/gotenberg_server_setup.md) | Full — corrected this pass: the doc previously instructed editing the now-unread `PDFTemplates` model to fix a font-rendering issue; the live template is `MediaTemplate(type='PDF')`. Following the old instructions would have silently done nothing. |

### 6. Operational / supporting modules

| Module | Source | Documentation | Coverage |
|---|---|---|---|
| Grab-bag utilities (S3, Elevate profile sync, chat/audio/SQL helpers, transliteration, media preview) | `chatbot/utils/` | [Utils](apps/chatbot/chatbot_utils.md) | Full |
| Management commands | `chatbot/management/commands/` | [Management Commands](apps/chatbot/chatbot_management.md) | Full |
| One-off / ops scripts | `chatbot/scripts/` | [Scripts](apps/chatbot/chatbot_scripts.md) | Full — note a pre-existing broken import in `lang_detect_eval.py` (`chatbot.scripts.lang_detect_sample_texts` does not exist in the repo). |
| Admin templates | `chatbot/templates/` | [Templates](apps/chatbot/chatbot_templates.md) | Full |

### 7. Integrations

| Module | Source | Documentation | Coverage |
|---|---|---|---|
| Qdrant vector database | External service, wired via `vector_service.py` | [API Documentation](integrations/vector_db/qdrant/api_documentation.md), [Developer Guide](integrations/vector_db/qdrant/developer_guide.md), [System Architecture](integrations/vector_db/qdrant/system_architecture.md), [Testing Guide](integrations/vector_db/qdrant/testing_guide.md) | Full |
