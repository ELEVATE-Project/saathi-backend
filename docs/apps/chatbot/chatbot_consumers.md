# Chatbot WebSocket Consumers

## Overview

The `consumers` module manages real-time WebSocket connections for the chatbot, enabling continuous interactive chat experiences. It handles message receipt, session management, and asynchronous processing initiation.

Only one flow is live: every websocket session — regardless of bot type (guided, oneshot, guest discussion, etc.) — connects through the single `ws/common/` route and is dispatched internally by `BotServiceFactory`/`ResponseHandlerFactory` (see [Strategies](chatbot_strategies.md) and [Services](chatbot_services.md)). The `consumers` directory previously held several flow-specific consumer classes (chaupal, free-flow, guided-guest, oneshot-guest, and various Bedrock-provider variants); those were confirmed unrouted (never registered in `chatbot/routing.py`'s `websocket_urlpatterns`) and removed.

## Consumers

### AsyncSocketConsumer

- Located in `chatbot/consumers/async_consumer.py`.
- The only consumer registered in `chatbot/routing.py` (`websocket_urlpatterns`), mounted at `ws/common/`.
- Extends `AsyncBaseConsumer` to implement core WebSocket lifecycle methods: connect, disconnect, and receive.
- Key features include:
  - Session and profile initialization on authentication messages.
  - Background task management using Celery for handling chat flow responses.
  - Message translation capabilities based on user-selected languages.
  - Asynchronous database operations for session creation and message logging.

### AsyncBaseConsumer

- Located in `chatbot/consumers/async_base_consumer.py`.
- Base class providing common async WebSocket consumer utilities that `AsyncSocketConsumer` extends.

## Downstream processing

`AsyncSocketConsumer` only authenticates the connection, persists the inbound
message, and enqueues a Celery task — it never calls the LLM or blocks on a
response. The full request path from there (Celery task → orchestrator →
strategy → response handler → LLM gateway → delivery back through this
consumer's `chat_message` handler), including a line-by-line breakdown of
`AsyncBaseConsumer`'s idle-timeout and disconnect bookkeeping and
`AsyncSocketConsumer`'s `authenticate`/message-translation logic, is covered
in depth in [WebSocket Response Handling](chatbot_response_handlers.md).
