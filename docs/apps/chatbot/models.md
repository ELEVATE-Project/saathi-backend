# Django Models

`chatbot/models/`

This layer defines the complete database schema for the chatbot platform.

It manages persistence, relationships, constraints, indexing, and domain-level behavior across users, bots, conversations, and configuration. (Story/Media/Theme/I18n domain models — knowledge-base document storage, vector indexing, tagging, story content — were removed as not part of Saathi's scope; see the repo-root `CODE_CLEANUP_PLAN.md`.)

---

Responsibilities of this Layer

- Define core domain entities (User, Bot, Company, Voice, Flow, etc.)
- Maintain relational integrity using ForeignKeys and constraints
- Enforce validation rules and uniqueness constraints
- Manage conversation state and session tracking
- Maintain historical tracking using `simple_history`
- Provide model-level helper methods for business logic
- Use enums for consistent state definitions

---

> **Note:** This reference has been backfilled to include every model currently defined under `chatbot/models/` — `CompanyChatFeedback`, `ImageConfiguration`, `PDFTemplates`, `MediaTemplate`, `Flow`, `Language`, `Provider`, and `LanguageProviderConfig` are documented below alongside the original ten, and the previously-documented `BotVernacular`, `ChatSession`, `CompanyBot`, `CompanyStateMachine`, and `Voice` entries have been corrected to match their current field sets. `generate_models_docs.py` (the script that originally produced this file's format) was removed as unused, so there is no automated way to regenerate this reference — a future field/model change can silently drift out of sync again unless this file is updated by hand alongside the model change.

## 1. BlacklistedToken

`chatbot/models/auth_models.py`

### Purpose

Stores authentication tokens that have been invalidated or revoked.
    Used to prevent blacklisted tokens from being reused.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| token | TextField (unique=True, required) |  |
| blacklisted_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_blacklisted_at()`
- `get_previous_by_blacklisted_at()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 2. BotVernacular

`chatbot/models/bot_vernacular_model.py`

### Purpose

Stores language-specific (vernacular) configurations for a company bot.
    Allows customized introductory and error messages per language.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| company_bot | ForeignKey (ForeignKey → CompanyBot) |  |
| language | CharField (required, max_length=250) | Language code, Example for English use en. |
| language_ref | ForeignKey (null, editable=False, ForeignKey → Language) | Structured language, auto-derived from `language` on save(). Not load-bearing — purely a derived convenience field; `language` remains the source of truth. |
| introductory_message | TextField () | Provide an introductory message that the bot will present when the conversation starts. |
| alt_introductory_message | TextField () | Provide an alternate introductory message that the bot will present when the conversation starts. |
| name | CharField (max_length=100) | Enter the name of the bot. |
| error_message | TextField () | Provide an error message that the bot will display. |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 3. ChatSession

`chatbot/models/chat_models.py`

### Purpose

Represents an active chat session between a user profile and a company bot.
    Stores session metadata, conversation state, and handles title generation using LLMs.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| session | CharField (unique=True, required, max_length=255) |  |
| profile | ForeignKey (ForeignKey → Profile) |  |
| company_bot | ForeignKey (ForeignKey → CompanyBot) |  |
| language | CharField (max_length=1000, default='en') | Language code — controlled at the admin form layer via a Language-table-sourced dropdown (`ChatSessionAdminForm`), not by a fixed model-level choice list. |
| language_ref | ForeignKey (null, editable=False, ForeignKey → Language) | Structured language, auto-derived from `language` on save(). Not load-bearing — purely a derived convenience field; `language` remains the source of truth. |
| title | CharField (max_length=255) |  |
| summary | TextField () |  |
| current_step | IntegerField () |  |
| session_context | JSONField () |  |
| session_status | CharField (max_length=20, choices) |  |
| project_id | CharField (max_length=400) |  |
| user_id | CharField (max_length=400) |  |
| session_type | CharField (max_length=255) |  |
| other_params | JSONField () |  |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_session_status_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_title()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 4. Company

`chatbot/models/company_models.py`

### Purpose

Represents a company that owns and manages chatbot configurations.
    Stores company details like name, slug, status, and logo.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| name | CharField (required, max_length=100) |  |
| slug | CharField (unique=True, required, max_length=100) |  |
| status | CharField (required, max_length=20, choices) |  |
| url | URLField (max_length=200) |  |
| logo | ImageField (max_length=1000) |  |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_file_upload_path()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_public_url()`
- `get_status_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 5. CompanyBot

`chatbot/models/company_models.py`

### Purpose

Defines a chatbot configuration for a specific company.
    Stores LLM settings, prompts, provider details, and behavior controls.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| name | CharField (required, max_length=100) | Enter the name of the bot. |
| company | ForeignKey (required, ForeignKey → Company) | Select the company this bot belongs to. |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |
| context | TextField (required) | Provide the bot's main prompt or description of its purpose. |
| max_token | IntegerField (required) |  |
| bot_temperature | FloatField (required) | Set the temperature for controlling response randomness (0-1). Lower values produce more deterministic responses. |
| top_k | IntegerField (required) | Set the top-k value for the bot's response selection. This defines how many top options to consider for each response. |
| provider | CharField (required, max_length=100, choices) | Legacy provider selector (BEDROCK, OPENAI, ANTHROPIC, or OPENROUTER) — superseded by `gateway_provider` for the actual LLM call. |
| provider_keys | TextField (required, max_length=1000) | API keys or credentials for the selected LLM provider. |
| llm_model | CharField (required, max_length=100, choices) | Select the LLM model to be used by the bot (e.g., GPT-4o, GPT-4). Legacy field — superseded by `gateway_provider`/`gateway_model` for the actual LLM call; see [Response Handlers](chatbot_response_handlers.md). |
| gateway_provider | CharField (max_length=100) | Select the LLM provider to use for the LLM Gateway call. Choices are fetched live from the LLM gateway. |
| gateway_model | CharField (max_length=150) | Select the model for the chosen gateway provider. If the provider was just changed, save the bot first — the model list updates to match the new provider after saving. |
| gateway_sub_provider | CharField (max_length=100) | Only used when `gateway_provider` is 'openrouter'. Selects which upstream endpoint (e.g. DeepInfra, Google, Anthropic) should serve the chosen model. |
| filter_score | FloatField (required) | Set the filter score for bot response selection (0-1). Responses below this score will be filtered out. |
| end_context | TextField () | Provide additional prompt or context to append at the end of the main prompt to guide the conversation |
| introductory_message | CharField (max_length=1000) | Provide an introductory message that the bot will present when the conversation starts. |
| tag_context | TextField () | Provide any information or context related to variables (like Python-bound variables) that will be inserted into the prompt. |
| route | CharField (required, max_length=100) | Specify the route or API endpoint for interacting with the bot. |
| bot_type | CharField (required, max_length=30, choices) |  |
| strategy | CharField (max_length=100, choices) | Select the strategy or approach this bot uses for conversations. |
| llm_key | CharField (max_length=255) |  |
| dynamic_context | TextField () | Provide dynamic context that can be adjusted during the bot's interactions, such as personalized data. |
| dynamic_context_type | CharField (max_length=20, choices) |  |
| pre_context | TextField () | Provide pre-context that will be set before the main prompt to shape the conversation. |
| tool_context | TextField () | JSON tool definitions for the LLM. For SIMPLE bots, the `search_knowledge_base` entry here is auto-added/removed based on `use_vector_service`. |
| other_params | JSONField () |  |
| connect_timeout | FloatField (required) | Timeout in seconds for establishing a LLM connection. |
| read_timeout | FloatField (required) | Timeout in seconds for reading a LLM response. |
| chat_history_limit | IntegerField (required) | Controls how many of the most recent chat messages are included as conversation history when making an LLM request. |
| stream | BooleanField (required) | Enable streaming mode for LLM responses. |
| use_vector_service | BooleanField (required) | Enable vector knowledge base search. Uses a two-step LLM call: first to extract the search query, then to answer with retrieved context. SIMPLE bots only: on save, this adds/removes the `search_knowledge_base` tool in `tool_context` automatically. |
| enable_web_search | BooleanField (required) | Enable web search via the LLM gateway. |
| web_search_context_size | CharField (required, max_length=10, choices) | Amount of context the web search retrieves. Only used when `enable_web_search` is True. |
| enable_cache | BooleanField (required) | Enable prompt/tool caching via the LLM gateway. When enabled, `cache_ttl` and `cache_targets` become required. |
| cache_ttl | CharField (max_length=20) | TTL to use for cached content. Choices are fetched live from the LLM gateway. Required when `enable_cache` is checked. |
| cache_targets | JSONField () | List of request parts to cache (e.g. `['prompt', 'tools']`). Choices are fetched live from the LLM gateway. Required when `enable_cache` is checked. |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_bot_type_display()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_dynamic_context_type_display()`
- `get_file_upload_path()`
- `get_llm_model_display()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_provider_display()`
- `get_strategy_display()`
- `get_web_search_context_size_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 6. CompanyChat

`chatbot/models/company_models.py`

### Purpose

Represents a chat message exchanged between a user and a company bot.
    Stores message content, session data, metadata, and optional attachments.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| message | TextField (required) |  |
| translated_message | TextField () |  |
| chunks | TextField () |  |
| sender | ForeignKey (ForeignKey → Profile) |  |
| receiver | ForeignKey (ForeignKey → Profile) |  |
| session | CharField (required, max_length=255) |  |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |
| status | CharField (max_length=20, choices) |  |
| feedback | CharField (max_length=20, choices) |  |
| source | CharField (required, max_length=20, choices) |  |
| source_msg_id | CharField (max_length=256) |  |
| whatsapp_message_id | CharField (max_length=255) |  |
| message_type | CharField (max_length=20) |  |
| stage | CharField (max_length=500) |  |
| other_params | JSONField () |  |
| audio_file | FileField (max_length=1000) |  |
| file_url | CharField (max_length=2000) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_feedback_display()`
- `get_file_upload_path()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_source_display()`
- `get_status_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 7. CompanyStateMachine

`chatbot/models/company_models.py`

### Purpose

Represents a step in a structured conversational workflow for a company bot.
    Defines stage logic, prompts, and optional pre/post processing rules.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| company_bot | ForeignKey (required, ForeignKey → CompanyBot) |  |
| name | CharField (required, max_length=100) | Enter the name of the state. |
| step | IntegerField (required) | Integer representing the order in which state function calling happens. Lower values are called first. |
| use_stage_chats | BooleanField (required) | If True, only chats from this stage will be included and passed to the LLM. |
| type | CharField (required, max_length=10, choices) | Specify whether the state is mandatory or optional. |
| text_conversion_type | CharField (required, max_length=15, choices) | Choose how to process this field's text: 'Translation' converts meaning into another language, 'Transliteration' preserves sound using another script. |
| bot_question | TextField () | Provide the first question that the bot will ask when the state is triggered. |
| completion_criteria | TextField () | Define the criteria required to move from this state to the next state. |
| context | TextField () | Provide the main prompt or description of the state, explaining its purpose. |
| tool_context | TextField () |  |
| preprocess_type | CharField (required, max_length=10, choices) | Choose how this stage should be preprocessed: 'Simple Prompt' lets you define a direct prompt, 'Use Preprocess Bot' lets you select a separate bot to handle complex logic. |
| preprocess_prompt | TextField () | Define the skip logic prompt if Preprocess Type is SIMPLE.  |
| preprocess_bot | ForeignKey (ForeignKey → CompanyBot) | Select which Bot to use for preprocessing for complex logic. |
| preprocess_output_mode | CharField (required, max_length=10, choices) | Define how to use the preprocess output: 'Skip' means use output to decide if stage should be skipped; 'Enrich' means use output in this stage's prompt; 'Custom' means run custom logic on the output. |
| postprocess_type | CharField (required, max_length=10, choices) | Choose how this stage should be postprocessed: 'Simple Prompt' lets you define a direct prompt, 'Use Postprocess Bot' lets you select a separate bot to handle complex logic. |
| postprocess_prompt | TextField () | Define the postprocess prompt if Postprocess Type is SIMPLE. |
| postprocess_bot | ForeignKey (ForeignKey → CompanyBot) | Select which Bot to use for postprocessing for complex logic. |
| postprocess_output_mode | CharField (required, max_length=10, choices) | Define how to use the postprocess output. |
| skip_to_step | IntegerField () | If set, the flow will skip directly to this step number when skip conditions are met. |
| operation_type | CharField (required, max_length=20, choices) | Choose whether this state uses LLM or non-LLM processing. |
| skip_if_authenticated | BooleanField (required) | If True, this state will be skipped for authenticated users. |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_operation_type_display()`
- `get_postprocess_output_mode_display()`
- `get_postprocess_type_display()`
- `get_preprocess_output_mode_display()`
- `get_preprocess_type_display()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_text_conversion_type_display()`
- `get_type_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 8. Profile

`chatbot/models/profile_models.py`

### Purpose

Represents a user profile associated with a company.
    Stores personal details, authentication data, and metadata for chatbot interactions.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| first_name | CharField (max_length=100) |  |
| userid | CharField (max_length=200) |  |
| last_name | CharField (max_length=100) |  |
| email | EmailField (required, max_length=1000) |  |
| phone | CharField (max_length=20) |  |
| alternate_phone | CharField (max_length=20) |  |
| country | CharField (max_length=100) |  |
| status | CharField (required, max_length=20, choices) |  |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |
| company | ForeignKey (required, ForeignKey → Company) |  |
| password | CharField (max_length=1000) |  |
| profile_type | CharField (required, max_length=20, choices) |  |
| profile_code | CharField (max_length=100) |  |
| location | CharField (max_length=1000) |  |
| caste | CharField (max_length=1000) |  |
| gender | CharField (max_length=1000, choices) |  |
| designation | TextField () |  |
| org_associated | CharField (max_length=1000) |  |
| product_interested | CharField (max_length=1000) |  |
| company_spoc | CharField (max_length=1000) |  |
| other_params | JSONField () |  |
| source | CharField (max_length=1000) |  |
| preferred_route | CharField (max_length=1000) |  |
| latest_flow_used | CharField (max_length=500, choices) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_file_upload_path()`
- `get_gender_display()`
- `get_latest_flow_used_display()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_profile_type_display()`
- `get_status_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 9. ProfileAddress

`chatbot/models/geo_models.py`

### Purpose

Stores address and geolocation details associated with a user profile.
    Includes full address fields along with optional latitude and longitude.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| profile | ForeignKey (required, ForeignKey → Profile) |  |
| address_line_1 | CharField (max_length=1000) |  |
| address_line_2 | CharField (max_length=1000) |  |
| block | CharField (max_length=1000) |  |
| city | CharField (max_length=1000) |  |
| district | CharField (max_length=1000) |  |
| state | CharField (max_length=1000) |  |
| country | CharField (max_length=1000) |  |
| pincode | CharField (max_length=10) |  |
| latitude | DecimalField () |  |
| longitude | DecimalField () |  |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 10. Voice

`chatbot/models/company_models.py`

### Purpose

Defines a text-to-speech voice configuration for a company bot.
    Stores provider details, language, gender, and playback settings.

A row can optionally be a fallback config for another (primary) Voice row, via `primary_voice` —
used to retry translation with a different provider if the primary one errors. `is_fallback` is
derived automatically from `primary_voice`. The default `objects` manager (`VoiceManager`) excludes
fallback-only rows (`is_fallback=False`); the unfiltered `all_voices` manager includes them.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| company_bot | ForeignKey (ForeignKey → CompanyBot) |  |
| type | CharField (max_length=300, choices) |  |
| provider | CharField (max_length=300, choices) | Legacy — frozen, hidden from admin, auto-synced from `provider_ref` on save(). Kept only for backward compatibility with existing read call sites. |
| name | CharField (max_length=100) |  |
| sample_link | URLField (max_length=200) |  |
| language | CharField (max_length=100) | Legacy — frozen, hidden from admin, auto-synced from `language_ref` on save(). Kept only for backward compatibility with existing read call sites. |
| provider_code | CharField (max_length=100) |  |
| language_ref | ForeignKey (required, ForeignKey → Language, on_delete=PROTECT) | Structured language for this voice config. |
| provider_ref | ForeignKey (required, ForeignKey → Provider, on_delete=PROTECT) | Structured provider for this voice config. |
| gender | CharField (required, max_length=100, choices) |  |
| voice_speed | FloatField () |  |
| primary_voice | OneToOneField (ForeignKey → Voice (self), related_name=fallback_config) | Set only on a row that is a fallback config for a Text To Text primary row. |
| is_fallback | BooleanField (editable=False) | Auto-derived from `primary_voice` — True for a row that is a fallback config. |
| other_params | JSONField () |  |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_gender_display()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_provider_display()`
- `get_type_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 11. CompanyChatFeedback

`chatbot/models/company_models.py`

### Purpose

A single feedback submission (thumbs up/down + optional comment) for a bot response.
    Rows are append-only — never updated — so the full history is preserved and the most
    recent row (by created_at) represents the current state.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| company_chat | ForeignKey (required, ForeignKey → CompanyChat, related_name=feedbacks) | The bot response (CompanyChat row) this feedback is about. |
| thumbs_up | BooleanField (required) | True if the user gave a positive rating in this submission. |
| thumbs_down | BooleanField (required) | True if the user gave a negative rating in this submission. Cannot be True at the same time as thumbs_up (enforced in the serializer). |
| comment | TextField () | Optional free-text feedback typed by the user. |
| created_at | DateTimeField (required) | When this feedback was submitted. Immutable — also used to determine the current state (latest row wins) and submission order. |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_previous_by_created_at()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 12. Flow

`chatbot/models/company_models.py`

### Purpose

Represents a conversation flow configuration.
    Links to a `CompanyBot`, and optionally to secondary bots (story, story-validation,
    title generation), a parent flow, and an image configuration.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| flow_name | CharField (required, max_length=255) | Name of the flow. |
| flow_route | CharField (required, unique=True, max_length=255) | Route/path for accessing this flow. |
| languages | JSONField (required, default=['en', 'hi', 'kn', 'te']) | List of supported language codes (e.g., ['en', 'hi', 'kn']). Must be a list of unique codes (enforced in `clean()`). |
| hidden | BooleanField (required) | If True, this flow will be hidden from public listing. |
| active | BooleanField (required) | If False, this flow will be disabled and not accessible. |
| bot | ForeignKey (required, ForeignKey → CompanyBot, related_name=flows) | The main bot associated with this flow. |
| story_bot | ForeignKey (ForeignKey → CompanyBot, related_name=story_flows) | Optional secondary bot for story-related functionality. |
| story_validation_bot | ForeignKey (ForeignKey → CompanyBot, related_name=story_validation_flows) | Optional secondary bot for story-related functionality. |
| title_bot | ForeignKey (ForeignKey → CompanyBot, related_name=title_flows) | Optional bot for session title generation. |
| websocket_url | CharField (required, max_length=500, default='ws/common/') | WebSocket path for real-time communication (e.g., ws/common). Do not include protocol or host. |
| parent_flow | ForeignKey (ForeignKey → Flow (self), related_name=child_flows) | Parent flow if this is a sub-flow. |
| user_type | CharField (required, max_length=20, choices) | User types allowed to access this flow (guest, auth, or all). |
| image_config | ForeignKey (ForeignKey → ImageConfiguration, related_name=flows) | Image configuration settings for this flow. |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_user_type_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 13. ImageConfiguration

`chatbot/models/company_models.py`

### Purpose

Configuration for image handling in flows and bots.
    Defines constraints like max images and per-image size limits.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| name | CharField (required, max_length=100) | Name for this image configuration. |
| max_images | IntegerField (required, default=1) | Maximum number of images allowed. |
| image_size | IntegerField (required, default=5242880) | Maximum image size in bytes (default: 5MB). |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 14. Language

`chatbot/models/language_provider_models.py`

### Purpose

A structured, DB-driven language definition (ISO 639 code plus display name), replacing
    the earlier pattern of hardcoded language choice lists scattered across models.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| iso_code | CharField (required, unique=True, max_length=10) | ISO 639 code, selected from the pycountry library dropdown (admin-form validated). |
| name | CharField (required, max_length=100) |  |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 15. LanguageProviderConfig

`chatbot/models/language_provider_models.py`

### Purpose

An override of a `Language`'s code for a specific `Provider`'s outbound API calls.
    Looked up live by (language, provider) — `Voice` holds no direct FK to a config row.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| language | ForeignKey (required, ForeignKey → Language, related_name=provider_configs) |  |
| provider | ForeignKey (required, ForeignKey → Provider, related_name=language_configs) |  |
| custom_code | CharField (max_length=20, default="") | Overrides the language's iso_code for this provider's outbound API calls. Leave blank to use iso_code as-is. |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

A unique constraint (`unique_language_provider`) enforces at most one config row per
(language, provider) pair.

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 16. MediaTemplate

`chatbot/models/company_models.py`

### Purpose

A generalized template for generating a downloadable document (PDF, DOCX, ...) for a flow.
    One row per (flow, type) — unlike `PDFTemplates` this covers any output format, since PDF
    (inline HTML/Jinja2 text) and DOCX (an uploaded .docx file rendered via docxtpl) need
    structurally different template storage. `type` decides which of `template`/`template_file`
    is used; `constants_json` and `flow` are shared/format-agnostic. See
    [Admin](chatbot_admin.md) and [Utils](chatbot_utils.md) for the admin and rendering behavior.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| type | CharField (required, max_length=10, choices) | Output format this template produces. |
| template_name | CharField (required, unique=True, max_length=255) | Unique name identifier for this template. |
| flow | ForeignKey (ForeignKey → Flow, related_name=media_templates) | Flow associated with this template. |
| constants_json | JSONField () | JSON object containing constants/variables used in the template. |
| template | TextField () | Template content (HTML/Jinja2) — used when type=PDF. |
| template_file | FileField (max_length=1000) | Uploaded .docx template (Jinja2 tags via docxtpl) — used when type=DOCX. |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_type_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 17. PDFTemplates

`chatbot/models/company_models.py`

### Purpose

Stores PDF templates used in flows, with dynamic content substitution.
    Superseded by `MediaTemplate` for new templates (the admin page for this model has been
    unregistered), but the model, table, and existing data remain untouched — see
    [Admin](chatbot_admin.md).

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| template | TextField (required) | Template content for PDF generation (e.g., HTML, EJS template). |
| template_name | CharField (required, unique=True, max_length=255) | Unique name identifier for this template. |
| user_type | CharField (required, max_length=20, choices) | User types that can use this template (guest, auth, or all). |
| constants_json | JSONField () | JSON object containing constants/variables used in the template. |
| flow | ForeignKey (ForeignKey → Flow, related_name=pdf_templates) | Flow associated with this template. |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `get_user_type_display()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`

---

## 18. Provider

`chatbot/models/language_provider_models.py`

### Purpose

A structured, DB-driven translation/speech provider definition. `slug` must match a key in
    `chatbot/constants/provider_dispatch.py` for TTS/STT/translate calls to actually work for
    that provider.

### Fields

| Field | Type & Constraints | Description |
|-------|-------------------|-------------|
| id | BigAutoField (unique=True, required) |  |
| name | CharField (required, unique=True, max_length=100) |  |
| slug | SlugField (unique=True, max_length=100) | Auto-generated from name on save() if left blank. Must match a key in `chatbot/constants/provider_dispatch.py` for TTS/STT/translate calls to actually work for this provider. |
| created_at | DateTimeField (required) |  |
| updated_at | DateTimeField (required) |  |

### Methods

- `DoesNotExist()`
- `MultipleObjectsReturned()`
- `adelete()`
- `arefresh_from_db()`
- `asave()`
- `check()`
- `clean()`
- `clean_fields()`
- `date_error_message()`
- `from_db()`
- `full_clean()`
- `get_constraints()`
- `get_deferred_fields()`
- `get_next_by_created_at()`
- `get_next_by_updated_at()`
- `get_previous_by_created_at()`
- `get_previous_by_updated_at()`
- `prepare_database_save()`
- `refresh_from_db()`
- `save_base()`
- `save_without_historical_record()`
- `serializable_value()`
- `unique_error_message()`
- `validate_constraints()`
- `validate_unique()`
