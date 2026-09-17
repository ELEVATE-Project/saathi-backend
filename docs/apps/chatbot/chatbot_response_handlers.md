# WebSocket Response Handling — Deep Dive

`chatbot/services/response_handlers/`

This document is the in-depth reference for the part of the chatbot pipeline that
turns an authenticated WebSocket message into an LLM call, a processed response,
and one or more messages sent back to the client. It covers, in order, the full
request path from the WebSocket consumer down to `BaseResponseHandler` and
`CommonResponseHandler`, since those two classes carry almost all of the
conversational logic in the application.

Related, higher-level documents: [Consumers](chatbot_consumers.md) (WebSocket
transport layer), [Services](chatbot_services.md) (orchestrator and supporting
services), [Strategies](chatbot_strategies.md) (the thin strategy layer that
selects a response handler), [Celery Tasks](chatbot_celery_tasks.md) (async
execution and message delivery), [Enums](../../backend/enums.md) (choice fields
referenced throughout this document).

---

## 1. Where this fits in the request path

Every chat turn — text arriving from the frontend over the single `ws/common/`
WebSocket route — passes through the following components in order:

```
Browser/App
   │  JSON over WebSocket
   ▼
AsyncSocketConsumer (chatbot/consumers/async_consumer.py)
   │  authenticates, persists the user's message, then
   │  enqueues a Celery task and returns immediately
   ▼
get_flow_response (Celery task, chatbot/celery_tasks/flow_tasks.py)
   │  builds a strategy + orchestrator, runs synchronously inside the worker
   ▼
ChatOrchestrator.process_chat_request (chatbot/services/core/orchestrator.py)
   │  gathers session/profile/bot data, builds the system prompt,
   │  then delegates response generation to the strategy
   ▼
CommonBotStrategy.get_response (chatbot/services/strategies/common_strategy.py)
   │  the only registered strategy; delegates directly to its response handler
   ▼
CommonResponseHandler.handle_response (chatbot/services/response_handlers/common_handler.py)
   │  implements the abstract hooks declared on BaseResponseHandler
   ▼
BaseResponseHandler.handle_response (chatbot/services/response_handlers/base_response_handler.py)
   │  runs preprocessing → LLM gateway call (with tool loop) → postprocessing,
   │  then calls back into CommonResponseHandler.process_response
   ▼
Channel layer send ("chat.message" / "chat_message" events)
   │
   ▼
AsyncBaseConsumer.chat_message → WebSocket send() → Browser/App
```

The consumer never talks to the LLM directly and the Celery task never talks to
the WebSocket directly — the two are decoupled through the Channels channel
layer. `channel_name` (the consumer's own channel, captured at the time the
Celery task is enqueued) is threaded through every layer below the consumer
specifically so that a background worker, running in a completely separate
process, can still deliver messages back to the one open socket that is waiting
for them.

---

## 2. WebSocket transport layer

### 2.1 Routing

`chatbot/routing.py` registers exactly one WebSocket route:

```python
websocket_urlpatterns = [
    re_path(r"ws/common/$", AsyncSocketConsumer.as_asgi()),
]
```

Every bot type (state-machine flows and simple bots alike) connects through this
single route. There is no per-flow or per-bot-type WebSocket endpoint; routing
between flows happens inside the message-processing pipeline via the `bot_route`
value sent in the `authenticate` message, not via the URL.

### 2.2 `AsyncBaseConsumer`

`chatbot/consumers/async_base_consumer.py` — abstract base class (extends
Channels' `AsyncWebsocketConsumer`) providing behavior shared by any WebSocket
consumer in the app, independent of chat-specific message parsing:

- **Idle timeout.** `connect()` starts a background `_idle_timeout_monitor()`
  task that polls every `WEBSOCKET_IDLE_POLL_INTERVAL` seconds (default 10) and
  force-closes the connection, sending an `idle_timeout` system event first,
  once `WEBSOCKET_IDLE_TIMEOUT` seconds (default 60) have elapsed since
  `self.last_activity` was last updated. Subclasses are responsible for bumping
  `self.last_activity` on every inbound message; `AsyncSocketConsumer.receive()`
  does this as its first statement.
- **Disconnect bookkeeping.** `disconnect()` cancels the idle-timeout task, then
  — if a session is known — asks Celery to generate a session title
  (`save_chat_session`, guarded so it only fires once, when the session has no
  title yet) and marks the last chat/session status as `PAUSED` via
  `determine_company_chat_status_async` / `update_last_chat_status_async`. All
  of this is wrapped in a broad `try/except` so a failure here can never prevent
  the socket from closing.
- **Chat status derivation** (`determine_company_chat_status`, sync, run inside
  `database_sync_to_async`): given a session, profile and route, returns one of
  the `ChatStatus` values (`STARTED`, `IN_PROGRESS`, `PAUSED`, `RESUME`,
  `COMPLETED`) based on whether any bot messages exist yet, whether the caller
  is disconnecting, and — for `STATE_MACHINE` bots — whether the current step is
  the last step in the flow. `SIMPLE` bots use a shorter rule (no notion of
  "last step"). On any exception this defaults to `PAUSED` rather than raising,
  since chat status is advisory, not authoritative state.
- **`chat_message(event)`** — the Channels event handler invoked when something
  sends `{"type": "chat.message", ...}` on this consumer's channel (Channels
  dispatches `chat.message` to a method named `chat_message`, replacing the dot
  with an underscore). This is the single delivery point through which
  Celery-task code (`translate_and_send_message`, `BaseResponseHandler._send_chunk`)
  gets tokens back to the actual open socket.
- **`receive()`** is declared abstract here (`raise NotImplementedError`) —
  `AsyncBaseConsumer` itself has no notion of chat message parsing.

### 2.3 `AsyncSocketConsumer`

`chatbot/consumers/async_consumer.py` — the only consumer actually registered in
`routing.py`, and the sole entry point for every chat interaction.

**Per-connection instance state** (set in `__init__`, populated during
`authenticate`): `session_id`, `profile_id`, `route` (target language, e.g.
`'en'`, `'hi'`), `bot_route` (which `CompanyBot.route` this connection talks
to), `company_bot`, `flow_name`, `ip_address`, `access_token`, `ums_profile`
(a snapshot of the caller's profile fetched from Elevate at authenticate time).

**Message shape.** The frontend sends JSON text frames. The first frame on a new
connection must have `type: "authenticate"`; every subsequent frame is a chat
message carrying at minimum a `text` field (optionally `asr_audio` for
speech-derived input).

**`authenticate` handling** (inside `receive()`):

1. Reads `sessionid`, `profileid`, `route`, `bot_route`, `flow_name`, `address`
   (IP), `access_token` off the payload.
2. Decodes the JWT in `access_token` against `JWT_PUBLIC_KEY` (HS256) purely to
   recover a `user_id` for bookkeeping — this is best-effort; a decode failure
   or missing key is logged, not fatal.
3. Calls out to Elevate (`fetch_elevate_user`, then `upsert_elevate_profile`) to
   resolve/create the local `Profile` from the caller's Elevate identity. Three
   failure modes each close the socket with a distinct `event` in the payload
   sent back first: `unauthorized` (bad/expired token) → `auth_error`, an
   Elevate-side 5xx → `service_error`, or an Elevate response with no
   `profileid` → `auth_error`. Only on success does `self.profile_id` get
   overwritten with the *Elevate-resolved* profile id (not necessarily the one
   the client sent), and `self.ums_profile` gets populated.
4. Resolves `self.company_bot` via `get_company_bot(profile, bot_route)` —
   company-scoped lookup when the profile has a company, otherwise a bare
   `CompanyBot.objects.get(route=bot_route)`.
5. `create_chat_session(...)` does a `get_or_create` on `ChatSession.session`.
   On first creation, `current_step` starts at the state machine step named
   `"CHALLENGES"` if the profile already has a `first_name` (skipping an
   onboarding step for a returning-looking user), else step 1. On reconnect to
   an *existing* session, it takes a row lock
   (`select_for_update()` inside `transaction.atomic()`) to update `language`
   (if the route changed) and merge `ip_address` into `other_params` — the lock
   exists specifically so this read-modify-write cannot race a Celery task from
   the previous turn that is concurrently writing `other_params` for usage
   tracking, finalized sources, or a pending text-conversion override (see
   §5.7).
6. If the session already existed (`session_created` is `False`), the chat
   status is recomputed and pushed onto `ChatSession.session_status` via
   `update_session_status`.

**Non-authenticate messages:**

1. Rejects with an `auth_required` system event (via `channel_layer.send`, not a
   direct `self.send`, for consistency with how bot replies are delivered) if
   `session_id`/`bot_route` are not yet set — i.e. `authenticate` was skipped or
   failed silently on the client side.
2. Recomputes chat status and echoes the user's own text back through the
   channel layer (`{"type": "chat_message", "text": {"msg": ..., "source": "user"}}`)
   — this round-trip through the channel layer (rather than a direct `self.send`)
   means the user's own echoed message goes through exactly the same
   `chat_message` handler as bot messages, keeping the wire format uniform.
3. **Inbound translation.** If `self.route != 'en'`, `translate_message()` is
   called on the raw user text before it is persisted or handed to the LLM —
   the LLM/state machine always operates on English text; only display to the
   end user is ever in the vernacular language. `translate_message()` first
   pops (and clears, under the same row lock discipline as above)
   `pending_text_conversion_type` from `ChatSession.other_params` — a one-shot
   override set by the *previous* bot turn via `respond_to_user`'s
   `next_reply_conversion` argument (see §5.7) — which, if present, forces
   transliteration instead of translation regardless of the state's static
   `text_conversion_type`. Absent an override, it falls back to the current
   `CompanyStateMachine.text_conversion_type` for that step. Transliteration
   uses a separate `Voice` row of `type=Transliterate`; translation uses one of
   `type=TextToText`. If no matching `Voice` row is configured for the route,
   the original message passes through untranslated.
4. Persists the (English, translated) message pair via
   `save_in_company_db` (`chatbot/celery_tasks/common_chat_tasks.py`), tagging
   it with the current state machine's `name` as `stage` when the bot is a
   `STATE_MACHINE` bot.
5. Enqueues `get_flow_response.delay(...)` — hardcoding `bot_type='common'`
   regardless of what `CompanyBot.strategy` might say, since `'common'` is the
   only strategy `BotServiceFactory` has registered (see §3) — and returns
   immediately. The consumer never blocks on the LLM call.

**`profile_update` event handler** — a second custom Channels event (alongside
`chat_message`) that this consumer listens for. It merges freshly-submitted
profile field updates into the live, in-memory `self.ums_profile` dict. This
exists so that `submit_user_context` (a tool call handled deep inside
`CommonResponseHandler`, §5.8) can push corrected onboarding fields — role,
school name, district, state — into the *current* connection's context
immediately, without waiting for the client to reconnect or re-fetch the
profile from Elevate.

---

## 3. Strategy and factory layer (thin dispatch)

This layer exists to keep the response-handler code swappable per bot type, but
today has exactly one branch on every axis:

- `BotServiceFactory` (`chatbot/services/core/bot_service_factory.py`) maps
  `bot_type -> BotStrategy` subclass. Only `'common' -> CommonBotStrategy` is
  registered. New bot types are added by calling
  `BotServiceFactory.register_strategy(bot_type, strategy_class)`.
- `BotStrategy` (`chatbot/services/strategies/base_strategy.py`) is an `ABC`
  requiring `get_default_route()`, `get_handler_type()`, `process_session()`,
  `get_response()`. Its `__init__` immediately resolves a response handler
  instance via `ResponseHandlerFactory.create_handler(self.get_handler_type())`
  and stores it as `self.response_handler`.
- `CommonBotStrategy` (`chatbot/services/strategies/common_strategy.py`)
  implements `get_handler_type()` as `'common'`, `process_session()` as a
  lookup of the `CompanyStateMachine` row for the session's `current_step`, and
  `get_response()` as a direct passthrough to
  `self.response_handler.handle_response(**kwargs)`.
- `ResponseHandlerFactory` (`chatbot/services/response_handlers/handler_factory.py`)
  maps `handler_type -> BaseResponseHandler` subclass. Only
  `'common' -> CommonResponseHandler` is registered, extended the same way via
  `register_handler(handler_type, handler_class)`.

In practice this means: adding a genuinely different conversational behavior
means writing a new `BotStrategy` + `BaseResponseHandler` pair and registering
both factories — the strategy layer itself does not need to change.

---

## 4. `ChatOrchestrator` — assembling the call

`ChatOrchestrator.process_chat_request` (`chatbot/services/core/orchestrator.py`)
is the synchronous entry point invoked by the `get_flow_response` Celery task.
Per turn, it:

1. Fetches session/profile/company-bot rows via `BaseChatService.get_session_data`.
2. Resolves bot vernacular + a (possibly first-name-personalized) introductory
   message via `BaseChatService.get_bot_vernacular_and_intro`.
3. Extracts a small profile-info dict (`first_name`, `user_location`) for
   one-shot/free-flow prompts via `BaseChatService.get_user_profile_info`.
4. Builds the initial `messages` array via `MessageHandler.prepare_messages`
   (wraps `chatbot/utils/chat_utils.get_guided_chat`).
5. Calls `bot_strategy.process_session(...)` — for `CommonBotStrategy`, this
   just resolves the current-step `CompanyStateMachine` row. A dict with an
   `'error'` key here short-circuits straight to `_handle_error_response`,
   which sends the error text to the client via
   `translate_and_send_message` and returns without ever reaching a response
   handler.
6. Re-filters chat history through `MessageHandler.get_filtered_chats` —
   scoped to just the current stage's messages when
   `CompanyStateMachine.use_stage_chats` is set, otherwise the full session
   history — and rebuilds `temp_messages` from that filtered set.
7. Builds the system prompt via `PromptBuilder.build_system_prompt` (bot
   context + state context + completion criteria + rendered `tag_context` +
   SQL-driven `dynamic_context`, each a Jinja2 template rendered against
   `{profile, address, ums_profile}` — undefined-variable errors are logged and
   swallowed, falling back to the unrendered template text rather than
   crashing the turn). If the session has accumulated `finalized_sources` in
   `other_params` (see §5.7), a rendered bullet list of them is appended to the
   prompt so the LLM knows what has already been finalized and can update it
   via `respond_to_user.finalized_sources` rather than re-deriving it from
   scratch every turn.
8. Assembles a `response_params` dict — `system_prompt`, `messages`,
   `company_bot`, `session_id`, `channel_name`, `language`, `profile_id`,
   `temp_messages`, `intro_mssg`, `access_token`, `ums_profile` — and calls
   `bot_strategy.get_response(**response_params)`, which lands in
   `BaseResponseHandler.handle_response`.

Any unhandled exception anywhere in this method is caught, logged, and turns
into a silent `None` return — the caller (the Celery task) has no further
error-recovery step, so an uncaught exception here means the user simply never
gets a reply for that turn (aside from whatever, if anything, was already
streamed to the socket before the exception).

---

## 5. `BaseResponseHandler` — the core response pipeline

`chatbot/services/response_handlers/base_response_handler.py` is an abstract
base class (~1200 lines) holding almost every piece of shared logic for turning
a prepared prompt into a delivered message: state-machine bookkeeping,
pre/postprocessing hooks, the LLM gateway call (streaming and non-streaming),
an in-process tool-execution loop, usage/cost accounting, and WebSocket
delivery helpers. Three methods are left abstract for subclasses to fill in:

```python
check_early_return(self, chat_session, **kwargs)
get_messages_for_llm(self, **kwargs)
process_response(self, response, chat_session, chunks, **kwargs)
```

`CommonResponseHandler` (§6) is the only concrete implementation.

### 5.1 `handle_response` — the top-level method

Called with the full `response_params` kwargs bag assembled by the
orchestrator, plus (from `CommonBotStrategy.process_session`) nothing extra —
state-machine lookup happens again, inside this method, from
`chat_session.current_step`. The method's control flow, in order:

1. **Early return.** `self.check_early_return(chat_session, **kwargs)` gives
   subclasses a chance to short-circuit before touching the LLM at all. Three
   shapes are recognized: a bare string (return it immediately, pipeline ends
   here), a dict with `skip_llm: True` (sets `kwargs['skip_llm'] = True` and
   falls through), or any other dict (treated as a function-call-shaped
   response — `is_function_call` is computed from it and it becomes the
   effective `response` further down if the LLM step is later skipped). `None`
   means "no early return, proceed normally."
   `CommonResponseHandler.check_early_return` uses this hook for one specific
   case: an `EVENT_DATE`-named state, which runs a heuristic date parser
   (`handle_date_prompt`) against chat history before ever calling the LLM
   (§6.2).
2. **NON_LLM state handling.** If the current `CompanyStateMachine.operation_type`
   is `OperationTypeChoices.NON_LLM` (`is_non_llm_state`), the LLM is always
   skipped (`skip_llm=True`, `skip_reason='non_llm_operation_type'`). Whether
   the state's canned `bot_question` still needs to be *asked* (first entry
   into the state — no prior `CompanyChat` row for this stage other than the
   question itself) or the state has already been asked and should now
   *advance* (`force_function_call=True`, synthesizing a
   `get_state_information` call via `build_non_llm_function_call`) is decided
   here by checking for existing `CompanyChat` rows tagged with this state's
   `stage` name, excluding the bot_question text itself.
3. **Preprocessing.** If the state's `preprocess_output_mode` is not one of
   `NONE`/`SKIP`/`MODIFY_QUESTION`... — note the actual guard is the inverse:
   preprocessing runs when the mode is *not* in that exclusion set, i.e. it
   runs for `SKIP` and `MODIFY_QUESTION` themselves and anything else outside
   the excluded set, but is skipped for the literal excluded values. See §7 for
   the full preprocessing service. Its `action` result can set `skip_llm=True`
   (`'skip'`), rewrite the system prompt (`'continue'`, prompt replaced), or
   substitute a modified bot question (`'modify_question'`) that later methods
   (`CommonResponseHandler._send_db_question`) will use instead of the state's
   static `bot_question`.
4. **LLM call.** Unless `is_function_call` was already resolved truthy from
   step 1 or `skip_llm` is set, `self.get_llm_response(**kwargs)` runs (§5.2).
   Its 3-tuple result — `(response, extra_content, finish_reason)` — is
   unpacked; `extra_content` may carry private (underscore-prefixed) keys
   pulled back out here: `_retrieved_chunks` (KB search results accumulated
   during the tool loop), `_usage_cost` (token/cost accounting for this turn),
   `_is_vernacular_error` (the fallback error message came pre-translated from
   `BotVernacular`, so it must bypass the normal translation step). Everything
   else on `extra_content` survives into `kwargs['llm_extra_content']` for the
   subclass's `process_response` to consume.
   A bare `None` response (the LLM gateway failed outright, not a deliberate
   empty answer) is turned into either a synthetic `get_state_information`
   fallback call (`STATE_MACHINE` bots — advances the flow rather than
   stalling it) or the bot's configured/default error message
   (non-`STATE_MACHINE` bots).
5. **Function-call detection + postprocessing.** `is_function_call` is
   (re-)computed from the final response via the subclass's `is_function_call`
   override. If true and a state machine applies,
   `PostprocessingService.execute_postprocessing` (§7) may set
   `skip_next_stage`/`target_stage`, which changes how many steps
   `CommonResponseHandler._handle_function_call` (§6.5) advances by. As a side
   effect, this also *pre-runs preprocessing for the next stage* here (not just
   at the top of the next turn) so a `MODIFY_QUESTION` result for that next
   stage is already available (`kwargs['modified_bot_question']`) by the time
   this turn's `process_response` call sends that stage's question.
6. **Delegate to subclass.** `self.process_response(response, chat_session,
   chunks, streaming_completed=streaming_completed, **kwargs)` — this is where
   all bot-type-specific behavior (state advancement, message persistence,
   translation, tool-specific branches) actually lives; see §6.
7. **Usage accounting.** If `kwargs['_usage_cost']` was populated in step 4,
   `_update_last_chat_usage` merges it into the most recently saved AI
   `CompanyChat` row's `other_params['usage']` after `process_response` has
   run (so that row already exists to attach usage to).

### 5.2 `get_llm_response` and the tool-context resolution

`get_llm_response` resolves which `tools` array (if any) to expose to the LLM
for this turn: from the current `CompanyStateMachine.tool_context` (parsed as
loose JSON via `json_repair`) when non-empty, or — only for `SIMPLE` bots —
from `CompanyBot.tool_context`. `STATE_MACHINE` bots with no `tool_context` set
on the current state simply get no tools that turn. It then hands off to
`_handle_gateway_response`, the actual model-calling + tool-execution loop.

### 5.3 `_handle_gateway_response` — the tool loop

This is the busiest method in the file (bounded at `max_iterations = 5`). Each
iteration:

1. Picks streaming vs. non-streaming (`should_use_streaming(company_bot)` — a
   plain `CompanyBot.stream` boolean — and only when `channel_name` is
   available) and calls `_handle_gateway_stream` or `_call_gateway_non_stream`
   accordingly (§5.4/§5.5).
2. If the result is a plain answer (no `function_call` key), several things
   can still happen before it's returned: a `respond_to_user`-handled empty
   response gets one retry with a synthetic tool-result message telling the
   model to answer as plain text before calling `respond_to_user` again; if
   still empty after that, a translated error message is returned instead; a
   genuinely empty *first-iteration* response with `enable_web_search` set but
   web search not yet tried gets one retry with web search turned on. Otherwise
   the tuple (with `turn_usage` merged in via `_with_turn_usage`) is returned
   straight up to `handle_response`.
3. If the result *is* a `function_call`:
   - Tools in `self._executable_tools` (currently just
     `search_knowledge_base`) are executed in-process via `_execute_tool`,
     which calls `chatbot.services.vector.vector_service.fetch_context_for_query`
     and formats retrieved chunks (or a "no result" placeholder, with
     different wording depending on whether web search is available as a
     fallback) into a synthetic tool-result message, which is appended to
     `current_messages` and fed back into the next iteration. The tool is then
     removed from `current_tools` so the model cannot call it again this turn.
   - Any other tool name is treated as a *pass-through* tool (not executable
     here) and the `function_call`-shaped result is returned immediately to
     `handle_response`, to be interpreted by `CommonResponseHandler.process_response`
     (§6.3) instead.
   - `web_search` is in `self._gateway_handled_tools` — it is never seen as a
     `function_call` at this layer at all; it is executed by the LLM gateway
     service itself (via `web_search_options` in the gateway params) and its
     results surface back as citations, not as a tool call this loop has to
     drive.
4. KB-search fallback logic: if the KB tool is present, web search starts
   disabled and only turns on once a KB search comes back with zero chunks
   (letting the model fall back to the open web); if there is no KB tool at
   all, web search respects `CompanyBot.enable_web_search` from the first
   iteration.

If all 5 iterations are exhausted without a final answer, the loop gives up and
returns the bot's error message with `finish_reason='stop'`.

### 5.4 Non-streaming gateway call — `_call_gateway_non_stream`

Calls `call_llm_gateway` (`chatbot/llm_models/llm_gateway.py`) with
provider/model resolved by `get_effective_provider_model` and params built by
`build_gateway_params` (max tokens, temperature, timeouts, `stop`/`seed`,
provider routing options, web-search/cache options — all sourced from
`CompanyBot` fields and `other_params`). The raw gateway JSON response is
interpreted in a specific priority order:

1. An **executable** tool call (`search_knowledge_base`) → returned as a
   `{'function_call': {...}}` shape with `finish_reason='function_call'`, to be
   picked up by the tool loop above.
2. A **`respond_to_user`** tool call *without* a legacy `response` argument key
   (the current tool contract) → the actual reply text is `message.content`
   (the model's plain-text answer), not a tool argument; the tool call itself
   only carries metadata (`quick_reply_chips`, `finalized_sources`,
   `next_reply_conversion`). See §5.7 for what happens to that metadata.
3. Any other (**pass-through**) tool call → returned as a `function_call` shape
   with no gateway-side interpretation.
4. Otherwise, `message.content` is the answer text directly, with citation
   chunks extracted from either `message['citations']` or
   `provider_specific_fields.web_search_results`, depending on which shape the
   underlying provider returned.

Token usage (`_extract_usage_cost`) is pulled from the raw response and both
accumulated into the running per-turn total (`turn_usage`, later attached as
`_usage_cost`) and immediately persisted into
`ChatSession.other_params['usage']` (session-lifetime totals, via
`_update_session_usage`, row-locked).

### 5.5 Streaming gateway call — `_handle_gateway_stream`

Consumes `call_llm_gateway_stream`, a generator yielding
`(delta_content, tool_use_delta, finish_reason, citations, finish_data)`
5-tuples parsed from the gateway's SSE stream. Plain text deltas are sent to
the WebSocket **as they arrive** via `_send_chunk` (type `"chunk"`, no
`finish_reason` until the last one) — this is why, for a `respond_to_user` or
plain-text turn, the user sees tokens appear incrementally rather than waiting
for the full response. Tool-call deltas are accumulated by index into
`tool_calls_buffer` (only index 0 is actually consumed once the stream ends —
this pipeline does not support multiple simultaneous tool calls per turn).
Once the stream ends:

- No tool call at all → the accumulated text is the final answer; it is saved
  to `CompanyChat` immediately (streaming already delivered it to the client,
  so persistence here is catch-up, not delivery) and a final empty `"chunk"`
  with `finish_reason` and any `sources` extra content is sent to close out the
  stream on the client side.
- `respond_to_user` with no legacy `response` key → same metadata handling as
  the non-streaming path, but the reply text was **already streamed
  token-by-token** during the loop, so the DB save + final "stop" chunk carry
  the metadata only, not a repeat of the text. An empty accumulated text here
  (model called `respond_to_user` without ever streaming any content) returns
  `finish_reason=None` specifically so `_handle_gateway_response`'s retry logic
  (§5.3, point 2) kicks in.
- Any other tool call → whatever text preamble streamed before the tool call
  is saved to `CompanyChat` as its own message, and
  `extra_for_tool['_text_streamed_to_ws'] = True` is set so that downstream
  code (`CommonResponseHandler._handle_freeflow_function_call`, §6.6) knows not
  to send that preamble text a second time when it later delivers the tool's
  own result (e.g. a download link).

### 5.6 Sources, citations and `is_function_call`

- `_prepare_sources` deduplicates retrieved/cited chunks into a `sources` list
  for `extra_content`, tagging each with `source: 'web_search'` (+ a bare
  domain, no `www.`/TLD, via `_extract_domain`) or `source: 'kb_search'` (+
  company name/logo when present on the chunk).
- `_extract_citation_chunks` / `_extract_citation_chunks_from_stream` normalize
  two different raw citation shapes (Anthropic-style nested
  `web_search_tool_result` objects vs. a flatter list-of-lists of citation
  dicts) into a single `{text, title, url, source: 'web_search'}` shape.
- `BaseResponseHandler.is_function_call` recognizes a `get_state_information`
  call across five different raw shapes a provider might return it in
  (`toolUseId`+`name`, `name`+`parameters`, `function_call`, `tool_calls`,
  Bedrock's nested `output.message.content[].toolUse`) — this breadth exists
  because different providers/SDK versions have historically serialized tool
  calls differently, and the gateway does not fully normalize this for the
  caller. `CommonResponseHandler.is_function_call` (§6.3) extends this further.

### 5.7 State persisted across turns via `ChatSession.other_params`

Three independent pieces of per-session state ride in the same JSON field, each
written under its own lock (`transaction.atomic()` +
`select_for_update()`) specifically so concurrent writers (a still-running
Celery task from the previous turn vs. the consumer handling the next inbound
message) cannot race each other:

| Key | Written by | Read/cleared by | Purpose |
|---|---|---|---|
| `usage` | `_update_session_usage` (base_response_handler) | (read-only, surfaced for reporting) | Running session-lifetime token/cost totals. |
| `finalized_sources` | `_save_finalized_sources` (base_response_handler) via `respond_to_user.finalized_sources`, or `_handle_json_tool_response` (common_handler) | `ChatOrchestrator` (injects into next turn's prompt); `_handle_freeflow_function_call`'s `download_file` handling (reads as the document's source list) | Lets the LLM incrementally curate a list of sources across a multi-turn conversation, surfaced back into its own next prompt so it can add/remove without re-deriving from scratch, and eventually gets embedded into a generated PDF/DOCX. |
| `pending_text_conversion_type` | `_save_pending_text_conversion` (base_response_handler) via `respond_to_user.next_reply_conversion` | `AsyncSocketConsumer.translate_message` (popped on the very next inbound message) | One-shot override: forces the *next* user message to be transliterated instead of translated (or vice versa), regardless of the state's static `text_conversion_type` — used when the model decides, e.g., a name or place needs sound-preserving transliteration for one turn only. |

### 5.8 WebSocket delivery helpers

- `_send_chunk(channel_name, content, finish_reason, extra_content=None)` —
  sends a `type: "chunk"` payload; used for both incremental streaming tokens
  (`finish_reason=None`) and the terminal chunk of a turn.
- `_send_error_chunk(channel_name, error_msg)` — sends a `type: "error"`
  payload; note this exists but `handle_response`'s own error paths
  predominantly go through `translate_and_send_message`
  (`chatbot/celery_tasks/handle_message.py`) instead, which also handles
  translation and quick-reply-chip translation before sending.
- Both are thin wrappers around `async_to_sync(channel_layer.send)(...)` with
  a `{"type": "chat.message", "text": {...}}` envelope — the same envelope
  shape `AsyncBaseConsumer.chat_message` expects.

---

## 6. `CommonResponseHandler` — the concrete implementation

`chatbot/services/response_handlers/common_handler.py` (~1100 lines) is the
only registered `BaseResponseHandler` subclass, and therefore implements 100%
of bot-facing conversational behavior for both `STATE_MACHINE` and `SIMPLE`
bots, and for the "free-flow" tool calls (`download_file`,
`submit_user_context`, `process_user_input`/`respond_to_user`-as-JSON) that
don't fit the state-machine advancement model at all.

### 6.1 `get_messages_for_llm`

Trivial: prefers `temp_messages` (the stage-filtered history built by
`ChatOrchestrator`) over the unfiltered `messages`, if present.

### 6.2 `check_early_return` and the `EVENT_DATE` special case

The only early-return special case in the codebase: a state literally named
`'EVENT_DATE'` runs `handle_date_prompt` (`chatbot/utils/shiksha_chaupal/date_utils.py`)
against the full chat history *before* any LLM call, to heuristically parse a
date out of the conversation without spending a model call on it. Three
outcomes: a parsed date closes out the state and returns a synthetic
`get_state_information` call advancing to state `'AUTO'`; a parse failure
(`None`) falls back to the bot's normal/vernacular error message; anything else
is treated as a follow-up question to send to the user (translated, saved,
returned as the final response for the turn — bypassing the LLM entirely for
that turn).

### 6.3 `is_function_call` — extended detection

Layers additional detection on top of the base class specifically to make
"the model produced no usable text" *behave like* a function call for
retry/postprocessing purposes, even when there is no actual tool call:

- A `function_call` whose name is *not* `get_state_information` is explicitly
  **not** a state-machine function call (important: `process_user_input`,
  `respond_to_user`, `submit_user_context`, `download_file` all reach this
  method too, and must not be misidentified as state advancement).
- OpenAI Responses-API-style `finish_reason == 'function_call'` and an explicit
  `should_function_call` flag (either top-level or nested inside the
  extracted response/metadata via `_extract_response_and_reason`) are both
  treated as true.
- An **empty extracted response string** (see `_extract_response_and_reason`
  below) is treated as a function call, on the theory that an empty answer
  from a `STATE_MACHINE` bot means the model has nothing more to say and the
  flow should advance rather than send a blank message.

### 6.4 `_extract_response_and_reason` — normalizing arbitrary provider shapes

A long, defensively-written parser that pulls a `(response_text, reason_text,
meta_dict)` triple out of *whatever shape* the LLM happened to return —
directly through six different possible nestings (`toolUseId`+`input`,
`name`+`parameters`, bare `parameters`, bare `input`, Bedrock's
`output.message.content[].toolUse.input`, OpenAI's `function_call`/
`tool_calls` with string-or-dict `arguments`), with `json.loads` first and
`json_repair.repair_json` as a fallback at every string-parsing step (models,
especially smaller/open ones, routinely emit near-valid JSON with trailing
commas, unescaped quotes, etc.). If a string response merely *looks* like JSON
missing its outer braces (contains `"response"` or `"reason"` with a colon), it
is patched into valid JSON before parsing is attempted. Genuinely unparseable
input falls back to treating the whole response as plain text.

### 6.5 `process_response` — the dispatch method

This is the concrete implementation of the abstract hook, and the busiest
branch point in the subclass. In order:

1. **Free-flow tool routing.** Before any state-machine logic, dict responses
   carrying a `function_call` are routed by name into one of three dedicated
   handlers, entirely bypassing the state-advancement path below:
   `download_file` → `_handle_freeflow_function_call` (§6.6);
   `process_user_input`/`respond_to_user` (only reached here for the legacy
   dict-with-`response`-key shape, since the metadata-only shape is handled
   inside `_call_gateway_non_stream`/`_handle_gateway_stream` already) →
   `_handle_json_tool_response`; `submit_user_context` →
   `_handle_profile_tool_response`.
2. **Short-response retry.** For non-function-call responses under
   `self.max_retry_attempts` (2) retries, a response under
   `self.min_word_count` (3) words triggers one re-call to the LLM
   (`get_llm_response`, recursing into `process_response` with the new
   result). Exhausting retries with the response still too short sets
   `kwargs['use_error_message']`, which substitutes the bot's configured error
   message for the final response.
3. **Dispatch by bot type.** A function call for a `STATE_MACHINE` bot goes to
   `_handle_function_call` (§6.7, advances `current_step`); a function call
   that is a dict with `function_call` but the bot is *not* `STATE_MACHINE`
   (free-flow bots calling something other than the three named tools above)
   also falls into `_handle_freeflow_function_call`; anything else is a
   regular text answer and goes to `_handle_regular_response` (§6.8).

### 6.6 `_handle_freeflow_function_call` and `download_file`

Handles tool calls that don't fit the state-machine model. The only
implemented tool today is `download_file`:

1. Reads `flow_name` from `ChatSession.session_type` (set once, from the
   WebSocket `authenticate` message's `flow_name`).
2. Reads the session's accumulated `finalized_sources` (§5.7) to embed as the
   document's reference list.
3. Translates every string/list-of-string/list-of-dict argument value (except
   `filename`) into the user's language via `_translate_download_arguments` —
   the LLM always produces document content in English; only the generated
   document itself is localized, per-field, at this point. The filename is
   translated separately (its own `_translate` call on the base name only,
   extension preserved).
4. Calls both `render_template_to_pdf` and `render_docx_from_template`
   (`chatbot/utils/media_preview/media_creation.py` — see
   [Utils](chatbot_utils.md) for the `MediaTemplate` model and rendering
   details) — **both formats are always attempted**, not just the one the user
   asked for, and both URLs (when available) are returned to the client
   together as `extra_content.download.{pdf_url, docx_url}`. If *both* fail on
   the first attempt, the whole pair is retried once before giving up.
5. If the model already streamed a text preamble before the tool call
   (`llm_extra_content._text_streamed_to_ws`, set in §5.5), only the download
   URLs are sent as a terminal "stop" chunk — the bot's confirmation text was
   already delivered. Otherwise the confirmation text
   (`arguments.bot_message`, or a generic fallback) is sent via
   `translate_and_send_message` along with the download links.
6. Always persists an `AI`-initiated `CompanyChat` row for this turn, with the
   download links recorded in `other_params.extra_content`, regardless of
   which delivery path above was taken.

`docx_result`/`create_docx_from_args` from an older hardcoded (non-template)
DOCX generation path still exist in `media_creation.py` for reference but are
no longer called here — DOCX generation now always goes through a configured
`MediaTemplate(type=DOCX)` row; there is no code fallback if none is
configured for the flow (see [Utils](chatbot_utils.md) and
[Admin](chatbot_admin.md) for how that template is authored).

### 6.7 `_handle_function_call` — state-machine advancement

Computes the next `current_step`: `+2` when either `skip_next_stage_preprocessing`
or an unqualified `skip_next_stage` (no explicit integer `target_stage`) is
set, otherwise `target_stage` directly (when postprocessing named one) or a
plain `+1`. If the new step is at or past the bot's total configured step
count, the session is marked `ChatStatus.COMPLETED`. It then looks up the
`CompanyStateMachine` row for the new step, uses its `bot_question` (or a
preprocessing-supplied `modified_bot_question` override) as the outgoing
message, translates and sends it, and persists it — tagging `other_params`
with `message_type: 'question'`/`operation_type: 'non_llm'`/`source:
'database'` specifically when the new state is itself `NON_LLM`, so the saved
row's provenance is distinguishable from an LLM-authored question. Returns
`None` (not the question text) if the target step has no matching
`CompanyStateMachine` row at all — this is the normal "end of flow" condition
when advancing past the last configured step.

### 6.8 `_handle_regular_response` and `_handle_response_extra_content`

For `SIMPLE` bots specifically, `_handle_response_extra_content` first
unwraps a dict response's `parameters`/`input` sub-dict up to the top level
(normalizing the same provider-shape variance `_extract_response_and_reason`
deals with, but for the plain-response path), then extracts a small
`extra_content` bundle: `query`, `should_move_forward`, `validation`, and
optionally `quick_reply_chips`. If `should_move_forward == 'yes'`, the visible
message is blanked out entirely (the state machine trusts the
`should_move_forward` signal to advance without showing intermediate
reasoning text to the user); otherwise, if the bot's `tag_context` JSON has an
entry keyed by the model's `validation` value, that canned message overrides
whatever `message`/`response` text the model produced. The final response text
(after this substitution), plus any `llm_extra_content` merged in from earlier
in the pipeline, is translated, saved (falling back to `"Understood."` if both
the text and any `query` extra are empty — the row must have *some* message
body), and returned. If `streaming_completed` was already `True` (the text was
fully delivered via streaming already), this method returns `None`
immediately after building `extra_content`, deliberately skipping the
duplicate translate+send it would otherwise perform — persistence for a
streamed turn already happened inside `_handle_gateway_stream` (§5.5).

### 6.9 `_handle_json_tool_response` and `_handle_profile_tool_response`

- `_handle_json_tool_response` — the legacy `process_user_input`/
  `respond_to_user` shape where the tool's own arguments dict *is* the
  response payload (as opposed to the current contract in §5.4 where the
  reply text lives in `message.content` and the tool call is metadata-only).
  Persists `finalized_sources` directly onto `ChatSession.other_params` if
  present, then forwards the arguments dict into `_handle_regular_response`
  for the same extraction/translation/save treatment as any other structured
  response.
- `_handle_profile_tool_response` — `submit_user_context`. Delegates to
  `_save_submitted_user_context`, which: marks
  `Profile.other_params['is_onboarding_completed'] = True`; pushes a
  `profile_update` Channels event to the live socket (§2.3) with a small
  field-name-mapped subset of the submitted arguments (`role`→`designation`,
  `school_name`→`org_associated`, `district`→`district`, `state`→`state`) so
  the *current* connection's in-memory profile is immediately consistent; and,
  given an `access_token`, pushes the same data to Elevate via
  `update_elevate_profile` (Elevate remains the system of record for profile
  data — this local write is a same-session convenience, not a replacement).
  The response text is forced to an empty string (no visible chat message for
  this tool), with the submitted profile echoed back only inside
  `extra_content.profile`/`profile_extracted`.

---

## 7. Preprocessing and postprocessing services

Two structurally identical, small factory-style services sit between the
prompt-building step and the LLM call (preprocessing) and between the LLM
response and state advancement (postprocessing). Both live under
`chatbot/services/{preprocessing,postprocessing}/` and are consumed only by
`BaseResponseHandler` (§5.1, §5.1 point 5).

- **`PreprocessingService`** (`preprocessing_service.py`) dispatches on
  `CompanyStateMachine.preprocess_type` (`PreProcessType.SIMPLE` →
  `SimplePreprocessor`, a single `preprocess_prompt`-driven LLM call;
  `PreProcessType.COMPLEX` → `ComplexPreprocessor`, a full second bot
  (`preprocess_bot`) invoked with its own system prompt) and then interprets
  the raw LLM output through `PreprocessOutputHandler`, keyed by
  `preprocess_output_mode`: `SKIP` (`SkipOutputHandler` — loosely scans the
  parsed JSON/text for a truthy boolean or a `"yes"`/`"true"`/`"skip"` string
  in any non-reasoning-named key) or `MODIFY_QUESTION`
  (`ModifyQuestionOutputHandler` — extracts a `modified_question` string).
  Both preprocessors call `chatbot.llm_models.llm_script.handle_bedrock_model`
  / `handle_openai_model` directly — **not** the LLM gateway used by the main
  response path (§5.4/§5.5) — so preprocessing/postprocessing LLM calls follow
  the older, provider-specific code path documented in
  [LLM Integration](../../backend/llm.md), independent of a bot's configured
  gateway provider/model.
- **`PostprocessingService`** (`postprocessing_service.py`) is the mirror image
  for `PostProcessType`/`postprocess_bot`/`postprocess_prompt`, but only
  implements the `SKIP` output mode (`PostprocessSkipOutputHandler`, same
  loose truthy-scan logic as its preprocessing counterpart) — there is no
  postprocessing equivalent of `MODIFY_QUESTION`.

Both services expose a `register_preprocessor`/`register_postprocessor` method
for adding new types without modifying the service class itself, mirroring the
extension pattern used throughout this layer (`BotServiceFactory`,
`ResponseHandlerFactory`).

---

## 8. Extending this layer

To add a genuinely new conversational behavior (as opposed to configuring an
existing one through `CompanyBot`/`CompanyStateMachine` admin fields):

1. Implement a new `BaseResponseHandler` subclass, filling in
   `check_early_return`, `get_messages_for_llm`, and `process_response`.
   Inherit everything else (the LLM gateway call, the tool loop, translation
   helpers, usage accounting) from the base class rather than reimplementing
   it.
2. Register it: `ResponseHandlerFactory.register_handler('<type>', NewHandler)`.
3. Implement a matching `BotStrategy` subclass whose `get_handler_type()`
   returns `'<type>'`, and register it via
   `BotServiceFactory.register_strategy('<bot_type>', NewStrategy)`.
4. Route to it: since `AsyncSocketConsumer.receive()` currently hardcodes
   `bot_type='common'` on every `get_flow_response.delay(...)` call, a second
   reachable bot type requires either changing that hardcoded value based on
   some per-connection signal (e.g. `CompanyBot.bot_type` or a new field), or
   adding a second WebSocket route in `chatbot/routing.py` dedicated to the new
   type — mirroring how `'common'` itself is wired end-to-end today.

Adding a new **tool** the model can call (as opposed to a new bot type) is
usually simpler: add it to `self._executable_tools` (if it should run
in-process during the tool loop, like `search_knowledge_base`) or handle its
name explicitly in `CommonResponseHandler._handle_freeflow_function_call`,
`_handle_json_tool_response`, or `process_response`'s dispatch table (if it's
a free-flow action or JSON-shaped tool instead) — and expose its schema to the
model via the relevant `CompanyStateMachine.tool_context`/`CompanyBot.tool_context`
JSON, not via a code change to the tool definitions module (which currently
only hosts the one always-available `search_knowledge_base` schema in
`chatbot/constants/tool_definitions.py`).
