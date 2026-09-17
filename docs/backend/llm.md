# Large Language Model (LLM) Integration

## Overview

The chatbot integrates with large language model providers through two
independent code paths that serve different callers:

- **The LLM gateway** (`chatbot/llm_models/llm_gateway.py`) — a thin HTTP
  client for an external LLM gateway service, used exclusively by
  `BaseResponseHandler` for the main conversational response on every chat
  turn (both streaming and non-streaming). This is the primary, actively
  developed path. See [WebSocket Response Handling](../apps/chatbot/chatbot_response_handlers.md)
  §5.2–§5.5 for how it is called, including the in-process tool-execution
  loop built around it.
- **Direct provider calls** (`chatbot/llm_models/llm_script.py`) — older,
  per-provider functions (`handle_bedrock_model`, `handle_openai_model`,
  `handle_llama_model`, `handle_openai_response_api`,
  `handle_bedrock_invoke_model`) called directly against AWS Bedrock or the
  OpenAI SDK, with no intermediary service. These are used today only by the
  preprocessing and postprocessing services
  (`chatbot/services/preprocessing/base_preprocessor.py`,
  `chatbot/services/postprocessing/base_postprocessor.py` — see
  [WebSocket Response Handling](../apps/chatbot/chatbot_response_handlers.md)
  §7), which run independently of a bot's configured gateway
  provider/model.

A bot's main response always goes through the gateway; only its optional
preprocessing/postprocessing steps (configured on `CompanyStateMachine` via
`preprocess_type`/`postprocess_type`) use the direct-call path.

## The LLM gateway (`chatbot/llm_models/llm_gateway.py`)

The gateway is a separate HTTP service (base URL: `LLM_GATEWAY_BASE_URL`,
authenticated via `LLM_GATEWAY_API_KEY` and `X-Tenant-Id: LLM_GATEWAY_TENANT_ID`)
that itself fronts multiple providers, so the chatbot backend never holds
provider API keys or provider-specific SDKs for this path.

- **`build_gateway_params(company_bot) -> dict`** — assembles the request
  `params` dict from `CompanyBot` fields: `max_tokens`, `temperature`,
  `connect_timeout`, `read_timeout`, plus optional `stop`/`seed`/`provider_options`
  from `other_params`, `web_search_options` (when `enable_web_search` is set,
  sized by `web_search_context_size`), and `cache_options` (when `enable_cache`
  is set, from `cache_ttl`/`cache_targets`). When `other_params.provider_options`
  is not explicitly set but the bot's `gateway_provider` is `'openrouter'` and
  a `gateway_sub_provider` is configured, a `provider_options.provider.only`
  override is derived automatically so the request routes to that specific
  upstream endpoint.
- **`get_effective_provider_model(company_bot) -> (provider, model)`** —
  resolves strictly from `gateway_provider`/`gateway_model`, with
  `other_params.custom_model` (a free-text override, for a model id not yet
  surfaced in the gateway's own catalog for that provider) taking priority
  over `gateway_model` when set. There is no fallback to the legacy
  `provider`/`llm_model` fields on `CompanyBot` for this call — those fields
  remain on the model for the direct-call path and for display purposes only.
- **`call_llm_gateway(messages, provider, model, params, tools, tool_choice)`** —
  `POST /v1/chat/` (non-streaming). Returns the raw parsed JSON response, or
  `None` on any network/HTTP failure (timeouts, HTTP errors, and unexpected
  exceptions are all caught and logged rather than raised).
- **`call_llm_gateway_stream(messages, provider, model, params, tools, tool_choice, cache_policy, metadata)`** —
  `POST /v1/chat/stream`, an SSE stream parsed into a generator of
  `(delta_content, tool_use_delta, finish_reason, citations, finish_data)`
  tuples, driven by three SSE event types: `token` (content delta),
  `tool_use` (a tool-call delta), `citation` (accumulated until the stream
  ends), and `finish` (terminal event carrying `finish_reason`, all
  accumulated citations, and the full finish payload for usage extraction).
- **Catalog/config helpers** — `get_provider_list()`, `get_model_list(provider)`,
  `get_openrouter_endpoints(model)`, `get_cache_options()` — read-only `GET`
  calls used to populate admin dropdowns (provider/model/cache selection on
  `CompanyBot`) rather than anything in the chat request path itself.

None of these functions raise on failure — every one logs and returns `None`
(or, for the stream generator, simply yields nothing further), so callers must
treat a `None`/empty result as "the call failed" rather than relying on an
exception.

## Direct provider calls (`chatbot/llm_models/llm_script.py`)

### AWS Bedrock

`handle_bedrock_model` sends conversation prompts to the AWS Bedrock Converse
endpoint and processes the response.

- **Parameters:** `messages` (chat messages and history), `max_token`,
  `model_name`, `is_json_format` (whether to expect JSON), `temperature`,
  `top_p`, `seed`, `n`, `stream` (sampling/streaming controls), `url_to_use`
  (optional endpoint override).
- **Output:** parsed JSON content when `is_json_format` is set, otherwise the
  raw string; retries and provider exceptions are handled internally.

`handle_bedrock_invoke_model` is a lower-level counterpart against the plain
Bedrock Invoke API (as opposed to Converse) for cases the Converse API does
not cover.

### OpenAI

`handle_openai_response_api` sends prompts to OpenAI's Responses API and
processes the result.

- **Parameters:** `messages` (starting with the system prompt), `max_token`,
  `temperature`, `company_bot` (optional context), `model_name` (falls back to
  the bot's configured model or a default), `is_json_response`, `stream`,
  `key_name`/`is_actual_key`/`client_choice` (API key/client selection),
  `tools`/`tool_choice`, `top_p`, `system_prompt`.
- **Output:** parsed JSON or plain string for non-streaming calls; a stream
  of partial responses when `stream=True`. Raises on error rather than
  swallowing it — unlike the gateway helpers above, callers here are expected
  to handle exceptions themselves.

`handle_openai_model` and `handle_llama_model` are the simpler
system-prompt/messages/temperature/max_token/tools call shapes actually used
by the preprocessing and postprocessing services (see the Overview section) —
narrower interfaces than `handle_openai_response_api`, without response-API-
specific features.

### Cost calculation

`get_pricing_from_company_bot`, `calculate_and_log_llm_cost`, and
`calculate_and_log_openai_cost` compute per-call token cost from a model's
configured pricing and log it; these are independent of the gateway's own
usage/cost reporting (`_extract_usage_cost` in `base_response_handler.py`),
which is what the main gateway-driven response path actually relies on for
`ChatSession`/`CompanyChat` usage accounting.
