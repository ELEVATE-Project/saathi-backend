from abc import ABC, abstractmethod
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.db import transaction
from chatbot.celery_tasks.common_chat_tasks import save_in_company_db
from chatbot.celery_tasks.handle_message import translate_and_send_message
from chatbot.llm_models.llm_gateway import (
    RAW_WEB_SEARCH_PROVIDERS, build_call_metadata, build_gateway_params, build_web_search_tool, call_llm_gateway,
    call_llm_gateway_stream, get_effective_provider_model, web_search_budget,
)
from chatbot.models import ChatSession, ChatStatus, CompanyBotTypeChoices
from chatbot.models.bot_vernacular_model import BotVernacular
from chatbot.models.company_models import CompanyStateMachine
from chatbot.models.enums import OperationTypeChoices, PreProcessOutputMode
from chatbot.services.postprocessing.postprocessing_service import PostprocessingService
from chatbot.services.preprocessing.preprocessing_service import PreprocessingService
import logging
import uuid

logger = logging.getLogger('django')
channel_layer = get_channel_layer()

# A safety refusal: 'content_filter' via LiteLLM, 'refusal' if the Anthropic stop_reason is passed through (T31).
REFUSAL_FINISH_REASONS = ('content_filter', 'refusal')


class BaseResponseHandler(ABC):
    """Base class for handling LLM responses with common functionality"""

    def __init__(self):
        self.default_error_message = 'I am sorry, I could not understood completely. Could you rephrase this please?'
        self.preprocessing_service = PreprocessingService()
        self.postprocessing_service = PostprocessingService()
        self._executable_tools = {'search_knowledge_base'}
        self._gateway_handled_tools = {'web_search'}
        self._metadata_tools = {'respond_to_user'}

    def get_error_message(self, company_bot, language):
        """Return (message, is_vernacular) — is_vernacular=True means message is already in target language."""
        try:
            vernacular = BotVernacular.objects.filter(company_bot=company_bot, language=language).first()
            if vernacular and vernacular.error_message:
                return vernacular.error_message, True
        except Exception as e:
            logger.error("Failed to fetch BotVernacular for language=%s: %s", language, e)
        return self.default_error_message, False

    def _log_if_truncated(self, finish_reason, session_id):
        """Log a reply cut off by the token limit. It is sent as it is, with no retry (T07)."""
        # The gateway passes 'length' (LiteLLM, OpenAI style) or 'max_tokens' (Anthropic direct).
        if finish_reason in ('length', 'max_tokens'):
            logger.warning(f'[gateway] reply truncated by max_tokens (finish_reason={finish_reason}) session={session_id}')

    def is_non_llm_state(self, state_machine):
        return (
                state_machine
                and hasattr(state_machine, 'operation_type')
                and state_machine.operation_type == OperationTypeChoices.NON_LLM
        )

    def build_non_llm_function_call(self, state_machine):
        return {
            "toolUseId": "non_llm_auto",
            "name": "get_state_information",
            "input": {
                "next_state_name": state_machine.name,
                "reason": "NON_LLM auto transition"
            }
        }

    def handle_response(self, **kwargs):
        """Main response handling method"""
        session_id = kwargs['session_id']
        chat_session = ChatSession.objects.get(session=session_id)
        chunks = []
        is_function_call = False
        # Prompt version when the turn starts; saved on its bot rows and sent on its LLM calls (T05).
        kwargs['bot_history_id'] = self._bot_history_id(kwargs.get('company_bot'))
        early_return = self.check_early_return(chat_session, **kwargs)
        if early_return is not None:
            if isinstance(early_return, str):
                return early_return
            elif isinstance(early_return, dict):
                if early_return.get('skip_llm', False):
                    kwargs['skip_llm'] = True
                else:
                    is_function_call = self.is_function_call(response=early_return)
            else:
                return early_return

        company_bot = kwargs.get('company_bot')
        try:
            state_machine = CompanyStateMachine.objects.filter(
                company_bot=company_bot, step=chat_session.current_step
            ).first()
        except Exception as e:
            logger.error(f"Error getting state machine: {e}")
            state_machine = None

        if self.is_non_llm_state(state_machine):
            from chatbot.models import CompanyChat

            user_messages_for_state = CompanyChat.objects.filter(
                session=session_id,
                stage=state_machine.name
            ).exclude(message=state_machine.bot_question).exists()

            kwargs['skip_llm'] = True
            kwargs['skip_reason'] = 'non_llm_operation_type'

            if not user_messages_for_state:
                kwargs['send_bot_question'] = True
                kwargs['bot_question_from_db'] = state_machine.bot_question or None
                logger.info(f"NON_LLM state {state_machine.name}: Asking question")

            else:
                kwargs['send_bot_question'] = False
                kwargs['force_function_call'] = True
                logger.info(f"NON_LLM state {state_machine.name}: Advancing to next state")

        original_prompt = kwargs.get('system_prompt', [])

        preprocessing_result = {'action': 'continue', 'prompt': original_prompt}
        if state_machine and state_machine.preprocess_output_mode not in [
            PreProcessOutputMode.NONE, PreProcessOutputMode.SKIP, PreProcessOutputMode.MODIFY_QUESTION
        ]:
            preprocessing_result = self.preprocessing_service.execute_preprocessing(
                state_machine, original_prompt, **kwargs
            )
            if preprocessing_result['action'] == 'skip':
                kwargs['skip_llm'] = True
                kwargs['skip_reason'] = 'preprocessing'
            elif preprocessing_result['action'] == 'modify_question':
                kwargs['modified_bot_question'] = preprocessing_result.get('modified_bot_question')
                kwargs['system_prompt'] = preprocessing_result.get('prompt', original_prompt)
                logger.info(f"Preprocessing modified bot_question: {kwargs['modified_bot_question']}")
            elif preprocessing_result['action'] == 'continue':
                kwargs['system_prompt'] = preprocessing_result.get('prompt', original_prompt)

        response = None
        streaming_completed = False
        turn_start_chat_id = None
        if not is_function_call and not kwargs.get('skip_llm', False):
            # Newest AI message before this turn: request ids go only to a message saved by this turn (T04).
            turn_start_chat_id = self._last_ai_chat_id(session_id)
            result = self.get_llm_response(**kwargs)

            if isinstance(result, tuple):
                response, extra_content, finish_reason = result

                # Pull out chunks accumulated during tool loop (KB search before pass-through tool)
                if isinstance(extra_content, dict) and '_retrieved_chunks' in extra_content:
                    chunks = extra_content.pop('_retrieved_chunks') or []

                if isinstance(extra_content, dict) and '_usage_cost' in extra_content:
                    kwargs['_usage_cost'] = extra_content.pop('_usage_cost')

                if isinstance(extra_content, dict) and '_llm_request_ids' in extra_content:
                    kwargs['_llm_request_ids'] = extra_content.pop('_llm_request_ids')

                if isinstance(extra_content, dict) and extra_content.pop('_is_vernacular_error', False):
                    kwargs['is_bot_vernacular_message'] = True

                # Store extra_content if present for later use
                if extra_content:
                    kwargs['llm_extra_content'] = extra_content
            else:
                response = result
                finish_reason = None

            use_streaming = self.should_use_streaming(company_bot)

            streaming_completed = finish_reason == "stop" and use_streaming

            # Only treat None as error
            if response is None:
                if company_bot.bot_type == CompanyBotTypeChoices.STATE_MACHINE:
                    response = {
                        "toolUseId": "tooluse_fallback", "name": "get_state_information",
                        "input": {
                            "next_state_name": "SAMPLE",
                            "reason": "LLM returned no response"
                        }
                    }
                else:
                    response, is_vernacular = self.get_error_message(company_bot, kwargs.get('language'))
                    if is_vernacular:
                        kwargs['is_bot_vernacular_message'] = True

        if is_function_call and response is None:
            response = early_return
        if kwargs.get('force_function_call') and state_machine:
            is_function_call = True
            response = self.build_non_llm_function_call(state_machine)

        if not is_function_call:
            is_function_call = self.is_function_call(response=response) if state_machine else False
        if is_function_call and state_machine and response:
            postprocessing_result = self.postprocessing_service.execute_postprocessing(
                state_machine, response, **kwargs
            )

            if postprocessing_result.get('skip_next_stage', False):
                kwargs['skip_next_stage'] = True
                kwargs['target_stage'] = state_machine.skip_to_step
                logger.info("Postprocessing will skip next stage")

                next_stage_number = kwargs['target_stage']
                try:
                    next_state_machine = CompanyStateMachine.objects.get(
                        company_bot=company_bot, step=next_stage_number
                    )

                    next_stage_preprocessing_result = self.preprocessing_service.execute_preprocessing(
                        next_state_machine, kwargs.get('system_prompt', []), **kwargs
                    )

                    if next_stage_preprocessing_result['action'] == 'skip':
                        kwargs['skip_next_stage_preprocessing'] = True
                    elif next_stage_preprocessing_result['action'] == 'modify_question':
                        kwargs['modified_bot_question'] = next_stage_preprocessing_result.get('modified_bot_question')
                        kwargs['system_prompt'] = next_stage_preprocessing_result.get('prompt', original_prompt)
                        logger.info(f"Preprocessing modified bot_question: {kwargs['modified_bot_question']}")

                except CompanyStateMachine.DoesNotExist:
                    logger.error(f"Next state machine {next_stage_number} not found for preprocessing")
            else:
                next_stage_number = chat_session.current_step + 1
                try:
                    next_state_machine = CompanyStateMachine.objects.get(
                        company_bot=company_bot, step=next_stage_number
                    )

                    next_stage_preprocessing_result = self.preprocessing_service.execute_preprocessing(
                        next_state_machine, kwargs.get('system_prompt', []), **kwargs
                    )

                    if next_stage_preprocessing_result['action'] == 'skip':
                        kwargs['skip_next_stage_preprocessing'] = True
                    elif next_stage_preprocessing_result['action'] == 'modify_question':
                        kwargs['modified_bot_question'] = next_stage_preprocessing_result.get('modified_bot_question')
                        kwargs['system_prompt'] = next_stage_preprocessing_result.get('prompt', original_prompt)
                        logger.info(f"Preprocessing modified bot_question: {kwargs['modified_bot_question']}")

                except CompanyStateMachine.DoesNotExist:
                    logger.info(f"Next state machine {next_stage_number} not found, likely at end of flow")

        process_result = self.process_response(
            response, chat_session, chunks, streaming_completed=streaming_completed, **kwargs
        )
        _usage_cost = kwargs.get('_usage_cost')
        _llm_request_ids = kwargs.get('_llm_request_ids')
        if _usage_cost or _llm_request_ids:
            self._update_last_chat_usage(
                session_id, _usage_cost, request_ids=_llm_request_ids, after_chat_id=turn_start_chat_id,
            )
        return process_result

    def analyze_response_for_postprocessing(self, response):
        """Analyze if response needs postprocessing - can be overridden by subclasses"""
        return self.is_function_call(response)

    def should_use_streaming(self, company_bot):
        """
        Determine if streaming should be used for this bot.
        """
        try:
            if hasattr(company_bot, 'stream'):
                return bool(company_bot.stream)

            return False

        except Exception as e:
            logger.error(f"Error determining streaming mode: {e}")
            return False

    def get_llm_response(self, **kwargs):
        """Get response from LLM provider"""
        company_bot = kwargs['company_bot']
        system_prompt = kwargs['system_prompt']
        response = None
        message_to_send = self.get_messages_for_llm(**kwargs)

        session_id = kwargs['session_id']
        profile_id = kwargs.get('profile_id')
        channel_name = kwargs['channel_name']
        chat_session = ChatSession.objects.get(session=session_id)
        state_machine = None
        try:
            state_machine = CompanyStateMachine.objects.get(
                company_bot=company_bot, step=chat_session.current_step
            )
        except CompanyStateMachine.DoesNotExist:
            # Expected for non-state-machine bots (e.g. SIMPLE) — they have no
            # per-step CompanyStateMachine row, so this just means no state
            # machine tool_context applies here.
            pass
        except Exception as e:
            logger.error(f"Error getting state machine for tools: {e}")

        tools = None
        has_state_machine_tool_context = (
                state_machine
                and hasattr(state_machine, 'tool_context')
                and state_machine.tool_context
                and state_machine.tool_context.strip()
        )

        has_company_bot_tool_context = (
                company_bot
                and hasattr(company_bot, 'tool_context')
                and company_bot.tool_context
                and company_bot.tool_context.strip()
        )

        if has_state_machine_tool_context or (
                company_bot.bot_type == CompanyBotTypeChoices.SIMPLE and has_company_bot_tool_context
        ):
            tool_context = (
                state_machine.tool_context.strip()
                if has_state_machine_tool_context
                else company_bot.tool_context.strip()
            )

            try:
                import json_repair
                tools = json_repair.repair_json(tool_context, return_objects=True)
                logger.info("Using state machine tool_context")
            except Exception as e:
                logger.error(f"Failed to parse state machine tool_context: {e}")
                tools = None

        result = self._handle_gateway_response(
            system_prompt=system_prompt, messages=message_to_send, company_bot=company_bot,
            session_id=session_id, profile_id=profile_id, tools=tools, channel_name=channel_name,
            language=kwargs.get('language'), bot_history_id=kwargs.get('bot_history_id'),
        )

        if result is None or (isinstance(result, tuple) and result[0] is None):
            logger.error("LLM gateway response returned None")
            extra = result[1] if isinstance(result, tuple) and isinstance(result[1], dict) else {}
            request_ids = extra.get('_llm_request_ids')
            return None, ({'_llm_request_ids': request_ids} if request_ids else None), None

        response, extra_content, finish_reason = result
        return response, extra_content, finish_reason


    def _handle_gateway_response(
        self, system_prompt, messages, company_bot, session_id, profile_id, tools=None, channel_name=None,
        language=None, bot_history_id=None,
    ):
        import json as _json

        tool_choice = None
        if isinstance(tools, dict):
            tool_choice = tools.get('tool_choice', 'auto')
            tools = tools.get('tools') or tools.get('tool')
        elif isinstance(tools, list):
            tool_choice = 'auto'

        gateway_messages = []
        if system_prompt:
            gateway_messages.append({'role': 'system', 'content': system_prompt})
        gateway_messages += messages

        other = company_bot.other_params or {}
        cache_policy = other.get('cache_policy')
        metadata_param = other.get('metadata')
        use_stream = self.should_use_streaming(company_bot) and channel_name

        current_messages = gateway_messages
        current_tools = tools
        current_tool_choice = tool_choice
        retrieved_chunks = []
        append_to_last = False
        max_iterations = 5
        turn_usage = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'cost_usd': 0.0, 'web_searches': 0,
                      'web_searches_unsure': 0}

        # T50: the tool list is the same on every call of the turn (a changing list makes the model drop plans
        # it made in call 1). Web search is offered from the first call when the bot has it on.
        use_web_search = getattr(company_bot, 'enable_web_search', False)
        kb_searched = False  # the KB tool ran once this turn; a repeat call gets a note, not a second search
        unoffered_answered = False  # a call to a tool that was not offered is answered once per turn
        empty_respond_to_user_retried = False
        text_streamed = False  # any text of this turn already sent to the client
        # T04: ids on every call of this turn, so each gateway ledger row joins to the chat.
        turn_id = uuid.uuid4().hex[:12]
        call_kind = 'answer'
        request_ids = []
        admin_metadata = metadata_param if isinstance(metadata_param, dict) else {}

        for iteration in range(max_iterations):
            call_metadata = build_call_metadata(
                session_id, company_bot, call_kind, turn_id=turn_id, language=language,
                bot_history_id=bot_history_id,
            )
            if use_stream:
                result = self._handle_gateway_stream(
                    gateway_messages=current_messages, company_bot=company_bot,
                    session_id=session_id, profile_id=profile_id, tools=current_tools,
                    tool_choice=current_tool_choice, channel_name=channel_name,
                    cache_policy=cache_policy, metadata={**admin_metadata, **call_metadata},
                    retrieved_chunks=retrieved_chunks, append_to_last=append_to_last,
                    use_web_search=use_web_search, turn_usage=turn_usage, request_ids=request_ids,
                    bot_history_id=bot_history_id,
                )
            else:
                result = self._call_gateway_non_stream(
                    gateway_messages=current_messages, company_bot=company_bot,
                    session_id=session_id, profile_id=profile_id,
                    tools=current_tools, tool_choice=current_tool_choice,
                    retrieved_chunks=retrieved_chunks, append_to_last=append_to_last,
                    use_web_search=use_web_search, turn_usage=turn_usage, request_ids=request_ids,
                    metadata=call_metadata,
                )

            print(f'[tool_loop] iteration={iteration} result={str(result)[:200]}')

            print("="*50)
            print("Result: ", result)
            print("="*50)

            if not result or (isinstance(result, tuple) and result[0] is None):
                # Keep the ids of the calls already made this turn, so the fallback reply links to them (T04).
                return None, ({'_llm_request_ids': list(request_ids)} if request_ids else None), None

            response_data, extra, finish = result

            if isinstance(extra, dict) and extra.get('_text_streamed_to_ws'):
                text_streamed = True

            if finish in REFUSAL_FINISH_REASONS:
                # No retry of any kind: the same request would be refused again (T31).
                # The refused call is the last id in this turn's llm_request_ids (T04).
                logger.warning(f'[tool_loop] model refusal (finish_reason={finish}) session={session_id} '
                               f'request_id={request_ids[-1] if request_ids else None} — sending error message')
                if use_stream and text_streamed:
                    # Tokens already sent can't be taken back: close that bubble so the error
                    # message shows as its own message, not glued to the earlier text.
                    self._send_chunk(channel_name, '', finish_reason='stop')
                err_msg, is_vernacular = self.get_error_message(company_bot, language)
                return self._with_turn_usage(
                    err_msg, {'_is_vernacular_error': is_vernacular} if is_vernacular else None, None, turn_usage,
                    request_ids,
                )

            # Thinking blocks must go back unchanged on the assistant turn that carries the tool call.
            thinking_blocks = extra.pop('_thinking_blocks', None) if isinstance(extra, dict) else None
            tool_call_id = extra.pop('_tool_call_id', None) if isinstance(extra, dict) else None
            # T50: web results found in the same response as a tool call; never passed on to process_response.
            web_chunks = extra.pop('_web_chunks', None) if isinstance(extra, dict) else None

            if not (isinstance(response_data, dict) and 'function_call' in response_data):
                if isinstance(extra, dict) and extra.pop('_respond_to_user_handled', False):
                    if not response_data and not empty_respond_to_user_retried:
                        empty_respond_to_user_retried = True
                        call_kind = 'retry'
                        logger.info('[tool_loop] respond_to_user called with no text — retrying with correction')
                        retry_assistant = {
                            'role': 'assistant', 'content': None,
                            'tool_calls': [{'id': 'tu_empty_retry', 'type': 'function',
                                            'function': {'name': 'respond_to_user', 'arguments': '{}'}}],
                        }
                        if thinking_blocks:
                            retry_assistant['thinking_blocks'] = thinking_blocks
                        current_messages = list(current_messages) + [
                            retry_assistant,
                            {
                                'role': 'tool', 'tool_call_id': 'tu_empty_retry',
                                'name': 'respond_to_user',
                                'content': 'Error: Response text is required. Write your full reply as plain text first, then call respond_to_user.',
                            },
                        ]
                        continue
                    if not response_data:
                        logger.info('[tool_loop] respond_to_user still empty after retry — sending default error')
                        err_msg, is_vernacular = self.get_error_message(company_bot, language)
                        return self._with_turn_usage(
                            err_msg, {'_is_vernacular_error': is_vernacular} if is_vernacular else None, None,
                            turn_usage, request_ids,
                        )
                    return self._with_turn_usage(response_data, extra, finish, turn_usage, request_ids)
                response_data, extra, finish = result
                return self._with_turn_usage(response_data, extra, finish, turn_usage, request_ids)

            tool_name = response_data['function_call'].get('name', '')
            arguments = response_data['function_call'].get('arguments', {})

            unoffered = bool(extra.pop('_unoffered_tool', False)) if isinstance(extra, dict) else False
            if unoffered and unoffered_answered:
                logger.warning(f'[tool_loop] model called not-offered tool={tool_name} again — ending the turn '
                               f'session={session_id}')
                if use_stream and text_streamed:
                    self._send_chunk(channel_name, '', finish_reason='stop')
                err_msg, is_vernacular = self.get_error_message(company_bot, language)
                return self._with_turn_usage(
                    err_msg, {'_is_vernacular_error': is_vernacular} if is_vernacular else None, None, turn_usage,
                    request_ids,
                )
            if not unoffered and tool_name not in self._executable_tools:
                logger.info(f'[tool_loop] passing through tool={tool_name} to process_response')
                response_data, extra, finish = result
                if retrieved_chunks:
                    extra = extra or {}
                    extra['_retrieved_chunks'] = retrieved_chunks
                return self._with_turn_usage(response_data, extra, finish, turn_usage, request_ids)

            if unoffered:
                unoffered_answered = True
                logger.warning(f'[tool_loop] model called not-offered tool={tool_name} session={session_id}')
                if tool_name in self._gateway_handled_tools:
                    tool_result_text = (
                        f'{tool_name} is not available in this chat. Answer without it; '
                        'do not say you searched online.'
                    )
                else:
                    tool_result_text = f'Tool "{tool_name}" is not available in this chat. Answer without it.'
                new_chunks = []
            elif tool_name == 'search_knowledge_base' and kb_searched:
                logger.info('[tool_loop] repeated search_knowledge_base call — not run again')
                tool_result_text = (
                    "You already searched Saathi's material this turn. Use the results above; "
                    'do not call search_knowledge_base again.'
                )
                new_chunks = []
            else:
                logger.info(f'[tool_loop] iteration={iteration} executing tool={tool_name}')
                tool_result_text, new_chunks = self._execute_tool(
                    tool_name=tool_name, arguments=arguments, company_bot=company_bot,
                )
                kb_searched = kb_searched or tool_name == 'search_knowledge_base'
                if tool_name == 'search_knowledge_base' and not new_chunks:
                    logger.info('[tool_loop] KB search found nothing')
            retrieved_chunks.extend(new_chunks or [])
            if web_chunks:
                # Keep the web results: for the final sources, and in this tool result so the model still sees them.
                retrieved_chunks.extend(web_chunks)
                web_parts = []
                for c in web_chunks:
                    header = f"[{c.get('title', '')}]({c['url']})" if c.get('url') else c.get('title', '')
                    web_parts.append(f"Source: {header}\n{c.get('text', '')}".rstrip())
                web_text = '\n'.join(web_parts)
                tool_result_text = f"{tool_result_text}\n\n{self._wrap_retrieved_content(web_text, 'web')}"

            tool_call_id = tool_call_id or f'tool_{iteration}'
            args_str = _json.dumps(arguments) if isinstance(arguments, dict) else (arguments or '{}')
            assistant_turn = {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': tool_call_id,
                    'type': 'function',
                    'function': {'name': tool_name, 'arguments': args_str},
                }],
            }
            if thinking_blocks:
                assistant_turn['thinking_blocks'] = thinking_blocks
            elif other.get('thinking'):
                logger.warning(f'[tool_loop] thinking is on but tool call {tool_name} came back with no thinking blocks session={session_id}')
            current_messages = list(current_messages) + [
                assistant_turn,
                {
                    'role': 'tool',
                    'tool_call_id': tool_call_id,
                    'name': tool_name,
                    'content': tool_result_text,
                },
            ]

            # T50: the tool list is not changed. The executed tool stays, so the list is the same on every call.
            current_tool_choice = 'auto' if current_tools else None
            append_to_last = True
            call_kind = 'tool'

        logger.error('[tool_loop] max tool iterations reached')
        err_msg, is_vernacular = self.get_error_message(company_bot, language)
        return self._with_turn_usage(
            err_msg, {'_is_vernacular_error': is_vernacular} if is_vernacular else None, 'stop',
            turn_usage, request_ids,
        )

    def _execute_tool(self, tool_name, arguments, company_bot):
        """Execute a tool call and return (result_text_for_llm, retrieved_chunks)."""
        if tool_name == 'search_knowledge_base':
            from chatbot.services.vector.vector_service import fetch_context_for_query
            query = arguments.get('query', '') if isinstance(arguments, dict) else ''
            _, retrieved_chunks = fetch_context_for_query(query=query, company_bot=company_bot)
            if retrieved_chunks:
                parts = []
                for c in retrieved_chunks:
                    title = c.get('title', '')
                    url = c.get('url', '')
                    text = c.get('text', '')
                    header = f'[{title}]({url})' if url else title
                    parts.append(f'Source: {header}\n{text}')
                chunks_content = '\n\n---\n\n'.join(parts)
                if getattr(company_bot, 'enable_web_search', False):
                    chunks_content += (
                        '\n\n---\n\n'
                        'These repository results passed the retrieval threshold, but check they actually '
                        'answer the query before using them. If they do not, web search is available as a '
                        'fallback.'
                    )
                chunks_text = self._wrap_retrieved_content(chunks_content, source='repository')
            else:
                if getattr(company_bot, 'enable_web_search', False):
                    no_result_message = (
                        'No repository result found for this query. Use web search to answer it. If web '
                        'search finds nothing useful either, answer from general knowledge and tell the user '
                        "the answer is not from Saathi's material or the web."
                    )
                else:
                    no_result_message = (
                        'No repository result found for this query. If you answer from general knowledge, '
                        "tell the user the answer is not from Saathi's material."
                    )
                chunks_text = self._wrap_retrieved_content(no_result_message, source='none')
            logger.info(f'[tool_loop] search_knowledge_base: {len(retrieved_chunks)} chunks for query: {query}')
            return chunks_text, retrieved_chunks

        logger.error(f'[tool_loop] unknown tool: {tool_name}')
        return f'Tool "{tool_name}" is not available.', []

    @staticmethod
    def _tool_names(tools):
        """Names of the tools in one request's tool list (function style or raw style)."""
        return {(t.get('function', {}).get('name') or t.get('name', '')) for t in (tools or []) if isinstance(t, dict)}

    def _is_unoffered_tool(self, name, tools, web_offered):
        """T50: True if the model called a tool this request did not offer. `tools` is the list sent to the gateway."""
        if name in self._metadata_tools:
            return False
        if name in self._gateway_handled_tools:
            return not web_offered
        return name not in self._tool_names(tools)

    def _wrap_retrieved_content(self, text, source):
        """Wrap tool-retrieved text in an explicit provenance marker before it enters the transcript."""
        return f'<retrieved_content source="{source}">\n{text}\n</retrieved_content>'

    def _call_gateway_non_stream(
        self, gateway_messages, company_bot, session_id, profile_id, tools, tool_choice,
        retrieved_chunks=None, append_to_last=False, use_web_search=False, turn_usage=None,
        request_ids=None, metadata=None,
    ):
        """Single non-streaming LLM call, returns (response, extra, finish_reason)."""
        import json
        try:
            params = build_gateway_params(company_bot)
            tools, searches_allowed = self._with_web_search_tool(company_bot, params, tools, use_web_search, turn_usage)
            web_offered = bool(searches_allowed) or (use_web_search and 'web_search_options' in params)
            print(f'[non_stream] use_web_search={use_web_search} bot.enable_web_search={getattr(company_bot, "enable_web_search", "N/A")} web_search_in_params={"web_search_options" in params}')
            effective_provider, effective_model = get_effective_provider_model(company_bot)
            data = call_llm_gateway(
                messages=gateway_messages, provider=effective_provider, model=effective_model,
                params=params, tools=tools, tool_choice=tool_choice, metadata=metadata,
            )
            logger.info(f"[gateway] raw response: {data}")
            if not data:
                return None, None, None
            if request_ids is not None and data.get('id'):
                request_ids.append(data['id'])

            usage_cost = self._extract_usage_cost(data)
            if usage_cost:
                self._update_session_usage(session_id, usage_cost)
                if turn_usage is not None:
                    self._accumulate_usage(turn_usage, usage_cost)

            choice = data.get('choices', [{}])[0]
            message = choice.get('message', {})
            finish_reason = choice.get('finish_reason')
            self._log_if_truncated(finish_reason, session_id)
            self._count_web_searches(
                turn_usage, searches_allowed, data.get('usage'), message.get('citations'),
                (message.get('provider_specific_fields') or {}).get('web_search_results'),
            )
            if finish_reason in REFUSAL_FINISH_REASONS:
                return '', None, finish_reason
            tool_calls = message.get('tool_calls') or []
            thinking_blocks = message.get('thinking_blocks')

            executable_tc = next(
                (tc for tc in tool_calls
                 if (tc.get('function', {}).get('name') or tc.get('name', '')) in self._executable_tools),
                None,
            )
            if executable_tc:
                raw_args = executable_tc.get('function', {}).get('arguments', '{}')
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                tool_extra = {'_tool_call_id': executable_tc.get('id')}
                web_chunks = self._extract_citation_chunks(message)  # T50: web results of this same response
                if web_chunks:
                    tool_extra['_web_chunks'] = web_chunks
                if thinking_blocks:
                    tool_extra['_thinking_blocks'] = thinking_blocks
                return {'function_call': {'name': executable_tc['function']['name'], 'arguments': arguments}}, tool_extra, 'function_call'

            # respond_to_user new format: LLM response text is in message.content; tool args are metadata only
            respond_to_user_tc = next(
                (tc for tc in tool_calls
                 if (tc.get('function', {}).get('name') or tc.get('name', '')) == 'respond_to_user'),
                None,
            )
            if respond_to_user_tc:
                raw_args = respond_to_user_tc.get('function', {}).get('arguments', '{}')
                ru_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if 'response' not in ru_args:
                    content = message.get('content', '') or ''
                    quick_reply_chips = self._parse_if_string(ru_args.get('quick_reply_chips'), [])
                    finalized_sources = self._parse_if_string(ru_args.get('finalized_sources'), [])
                    next_reply_conversion = ru_args.get('next_reply_conversion')

                    if finalized_sources is not None:
                        self._save_finalized_sources(session_id, finalized_sources)
                    if content and next_reply_conversion:
                        self._save_pending_text_conversion(session_id, next_reply_conversion)

                    citation_chunks = self._extract_citation_chunks(message)
                    all_chunks = list(retrieved_chunks or []) + citation_chunks
                    sources = self._prepare_sources(all_chunks)
                    extra_content = {}
                    if sources:
                        extra_content['sources'] = sources
                    if quick_reply_chips is not None:
                        extra_content['quick_reply_chips'] = quick_reply_chips

                    if all_chunks:
                        extra_content['_retrieved_chunks'] = all_chunks
                    if thinking_blocks:
                        extra_content['_thinking_blocks'] = thinking_blocks

                    extra_content['_respond_to_user_handled'] = True
                    return content, extra_content or None, finish_reason
                # old format (has 'response' key) — falls through to passthrough_tc below

            # Pass-through tools: offered in this call, not executable by us, not handled by the gateway
            # → return to process_response
            passthrough_tc = next(
                (tc for tc in tool_calls
                 if (tc.get('function', {}).get('name') or tc.get('name', '')) not in self._gateway_handled_tools
                 and not self._is_unoffered_tool(
                     tc.get('function', {}).get('name') or tc.get('name', ''), tools, web_offered)),
                None,
            )
            if passthrough_tc:
                tc_name = passthrough_tc.get('function', {}).get('name') or passthrough_tc.get('name', '')
                raw_args = passthrough_tc.get('function', {}).get('arguments', '{}')
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                return {'function_call': {'name': tc_name, 'arguments': arguments}}, None, 'function_call'

            # T50: a call to a tool this request did not offer (web_search with web search off, or a made-up name).
            # The tool loop answers it with an error result; it must not fall through to the text below.
            unoffered_tc = next(
                (tc for tc in tool_calls
                 if self._is_unoffered_tool(tc.get('function', {}).get('name') or tc.get('name', ''), tools, web_offered)),
                None,
            )
            if unoffered_tc:
                tc_name = unoffered_tc.get('function', {}).get('name') or unoffered_tc.get('name', '')
                raw_args = unoffered_tc.get('function', {}).get('arguments', '{}')
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    arguments = {}
                unoffered_extra = {'_unoffered_tool': True, '_tool_call_id': unoffered_tc.get('id')}
                web_chunks = self._extract_citation_chunks(message)
                if web_chunks:
                    unoffered_extra['_web_chunks'] = web_chunks
                if thinking_blocks:
                    unoffered_extra['_thinking_blocks'] = thinking_blocks
                return {'function_call': {'name': tc_name, 'arguments': arguments}}, unoffered_extra, 'function_call'

            content = message.get('content') or ''
            print(f'[non_stream_response] finish_reason={finish_reason} content={repr(content[:200])}')

            citation_chunks = self._extract_citation_chunks(message)
            all_chunks = list(retrieved_chunks or []) + citation_chunks

            # No save here: content still flows to _handle_regular_response, which does the canonical save.
            sources = self._prepare_sources(all_chunks)
            extra = {'sources': sources} if sources else None
            if all_chunks:
                extra = extra or {}
                extra['_retrieved_chunks'] = all_chunks
            return content, extra, finish_reason

        except Exception as e:
            logger.error(f'Error in non-stream gateway call: {e}', exc_info=True)
            return None, None, None


    def _handle_gateway_stream(
        self, gateway_messages, company_bot, session_id, profile_id, tools, tool_choice,
        channel_name, cache_policy=None, metadata=None, retrieved_chunks=None, append_to_last=False,
        use_web_search=False, turn_usage=None, request_ids=None, bot_history_id=None,
    ):
        import json
        accumulated_content = []
        tool_calls_buffer = {}  # index -> {id, name, arguments}
        finish_reason = None
        try:
            stream_params = build_gateway_params(company_bot)
            tools, searches_allowed = self._with_web_search_tool(
                company_bot, stream_params, tools, use_web_search, turn_usage,
            )
            web_offered = bool(searches_allowed) or (use_web_search and 'web_search_options' in stream_params)
            print(f'[stream] use_web_search={use_web_search} bot.enable_web_search={getattr(company_bot, "enable_web_search", "N/A")} web_search_in_params={"web_search_options" in stream_params}')
            citation_chunks = []
            citation_events = []
            finish_chunk = None
            effective_provider, effective_model = get_effective_provider_model(company_bot)
            for delta_content, tool_use_delta, chunk_finish_reason, chunk_citations, chunk_finish_data in call_llm_gateway_stream(
                messages=gateway_messages, provider=effective_provider, model=effective_model,
                params=stream_params, tools=tools, tool_choice=tool_choice,
                cache_policy=cache_policy, metadata=metadata,
            ):
                if delta_content:
                    self._send_chunk(channel_name, delta_content, finish_reason=None)
                    accumulated_content.append(delta_content)
                if tool_use_delta:
                    idx = tool_use_delta.get('index', 0)
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {'id': '', 'name': '', 'arguments': ''}
                    if tool_use_delta.get('id'):
                        tool_calls_buffer[idx]['id'] = tool_use_delta['id']
                    if tool_use_delta.get('name'):
                        tool_calls_buffer[idx]['name'] = tool_use_delta['name']
                    tool_calls_buffer[idx]['arguments'] += tool_use_delta.get('arguments_delta') or ''
                if chunk_citations:
                    citation_events.extend(chunk_citations)
                    citation_chunks.extend(self._extract_citation_chunks_from_stream(chunk_citations))
                if chunk_finish_reason:
                    finish_reason = chunk_finish_reason
                if chunk_finish_data:
                    finish_chunk = chunk_finish_data
            self._log_if_truncated(finish_reason, session_id)
            finish_data = finish_chunk or {}
            self._count_web_searches(
                turn_usage, searches_allowed, finish_data.get('usage'),
                citation_events or finish_data.get('citations'), None,
            )
            if request_ids is not None and (finish_chunk or {}).get('id'):
                request_ids.append(finish_chunk['id'])

            usage_cost = self._extract_usage_cost(finish_chunk)
            if usage_cost:
                self._update_session_usage(session_id, usage_cost)
                if turn_usage is not None:
                    self._accumulate_usage(turn_usage, usage_cost)

            if finish_reason in REFUSAL_FINISH_REASONS:
                # Nothing is saved; the tool loop sends the error message instead (T31).
                return '', {'_text_streamed_to_ws': True} if accumulated_content else None, finish_reason
            thinking_blocks = (finish_chunk or {}).get('thinking_blocks')

            if tool_calls_buffer:
                tc = tool_calls_buffer[0]
                raw_args = tc['arguments']
                print(f'[stream_tool_call] name={tc["name"]} args={raw_args[:200]}')
                try:
                    tc_arguments = json.loads(raw_args)
                except json.JSONDecodeError:
                    import json_repair
                    tc_arguments = json_repair.repair_json(raw_args, return_objects=True)
                if not isinstance(tc_arguments, dict):
                    tc_arguments = {}

                if tc['name'] == 'respond_to_user' and 'response' not in tc_arguments:
                    # New format: text was already streamed token-by-token; tool call carries metadata only
                    content = ''.join(accumulated_content)
                    quick_reply_chips = self._parse_if_string(tc_arguments.get('quick_reply_chips'), [])
                    finalized_sources = self._parse_if_string(tc_arguments.get('finalized_sources'), [])
                    next_reply_conversion = tc_arguments.get('next_reply_conversion')

                    if finalized_sources is not None:
                        self._save_finalized_sources(session_id, finalized_sources)
                    if content and next_reply_conversion:
                        self._save_pending_text_conversion(session_id, next_reply_conversion)

                    all_chunks = list(retrieved_chunks or []) + citation_chunks
                    sources = self._prepare_sources(all_chunks)
                    extra_content = {}
                    if sources:
                        extra_content['sources'] = sources
                    if quick_reply_chips is not None:
                        extra_content['quick_reply_chips'] = quick_reply_chips
                    # Marker consumed by _handle_gateway_response to skip web-search retry
                    extra_content['_respond_to_user_handled'] = True

                    if content:
                        # Text was streamed token-by-token; save to DB and send stop chunk with chips
                        save_in_company_db(
                            session_id=session_id, profile_id=profile_id, initiated_by='AI',
                            message=content, chunks=all_chunks, status=ChatStatus.IN_PROGRESS,
                            stage=None, append_to_last=append_to_last,
                            other_params={'usage': usage_cost} if usage_cost else None,
                            bot_history_id=bot_history_id,
                        )
                        self._send_chunk(channel_name, '', finish_reason='stop',
                                         extra_content={k: v for k, v in extra_content.items()
                                                        if k != '_respond_to_user_handled'} or None)
                        return content, extra_content, 'stop'
                    else:
                        # LLM generated no text — signal _handle_gateway_response to retry with correction.
                        # Thinking blocks are added only here, after nothing was sent to the client.
                        if thinking_blocks:
                            extra_content['_thinking_blocks'] = thinking_blocks
                        return content, extra_content, None

                # Any other tool call — preamble was already sent to WS token-by-token; only save to DB
                preamble_streamed = bool(accumulated_content)
                if accumulated_content:
                    preamble = ''.join(accumulated_content)
                    save_in_company_db(
                        session_id=session_id, profile_id=profile_id, initiated_by='AI',
                        message=preamble, chunks=None, status=ChatStatus.IN_PROGRESS, stage=None,
                        bot_history_id=bot_history_id,
                    )
                all_chunks_for_tool = list(retrieved_chunks or []) + citation_chunks
                extra_for_tool = {}
                if all_chunks_for_tool:
                    extra_for_tool['_retrieved_chunks'] = all_chunks_for_tool
                if preamble_streamed:
                    # Signal to process_response handlers that text was already sent to WS
                    extra_for_tool['_text_streamed_to_ws'] = True
                unoffered = self._is_unoffered_tool(tc['name'], tools, web_offered)  # T50
                if tc['name'] in self._executable_tools or unoffered:
                    extra_for_tool['_tool_call_id'] = tc['id'] or None
                    if thinking_blocks:
                        extra_for_tool['_thinking_blocks'] = thinking_blocks
                if unoffered:
                    extra_for_tool['_unoffered_tool'] = True
                if citation_chunks and (tc['name'] in self._executable_tools or unoffered):
                    extra_for_tool['_web_chunks'] = citation_chunks  # T50: kept by the tool loop
                extra_for_tool = extra_for_tool or None
                return {'function_call': {'name': tc['name'], 'arguments': tc_arguments}}, extra_for_tool, 'function_call'

            # No tool call — content was already streamed token-by-token; save to DB
            content = ''.join(accumulated_content)
            logger.info(f"[gateway] raw stream response: {content}")
            all_chunks = list(retrieved_chunks or []) + citation_chunks
            if content:
                save_in_company_db(
                    session_id=session_id, profile_id=profile_id, initiated_by='AI',
                    message=content, chunks=all_chunks, status=ChatStatus.IN_PROGRESS, stage=None,
                    append_to_last=append_to_last,
                    other_params={'usage': usage_cost} if usage_cost else None,
                    bot_history_id=bot_history_id,
                )
            sources = self._prepare_sources(all_chunks)
            extra_content = {'sources': sources} if sources else None
            self._send_chunk(channel_name, '', finish_reason=finish_reason or 'stop', extra_content=extra_content)
            return content, extra_content, finish_reason or 'stop'

        except Exception as e:
            logger.error(f'Error in gateway stream handling: {e}', exc_info=True)
            return None, None, None

    def _prepare_sources(self, chunks):
        """Build deduplicated sources list from retrieved chunks for extra_content."""
        print("="*100)
        print("Chunks: ", chunks)
        print("="*100)
        if not chunks:
            return []
        seen = set()
        sources = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            title = chunk.get('title', '')
            url = chunk.get('url', '')
            key = url or title
            if not key or key in seen:
                continue
            seen.add(key)
            if url:
                source_entry = {'title': title, 'url': url}
            else:
                source_entry = {'title': f'Referred: {title}'}

            chunk_source = chunk.get('source', '')
            if chunk_source == 'web_search':
                source_entry['source'] = 'web_search'
                domain = self._extract_domain(url)
                if domain:
                    source_entry['domain'] = domain
            elif chunk_source == 'kb_search':
                source_entry['source'] = 'kb_search'
                company = chunk.get('company', '')
                if company:
                    source_entry['company'] = company
                logo = chunk.get('logo', '')
                if logo:
                    source_entry['logo'] = logo

            sources.append(source_entry)
        return sources

    @staticmethod
    def _extract_domain(url):
        """Return just the site name from a URL's domain — no 'www.' prefix, no TLD (e.g. 'impriindia')."""
        if not url:
            return ''
        from urllib.parse import urlparse
        netloc = urlparse(url).netloc
        if netloc.startswith('www.'):
            netloc = netloc[len('www.'):]
        return netloc.split('.')[0]

    def _extract_citation_chunks(self, message):
        """Extract web search citations from a non-stream gateway message and return as chunk dicts."""
        chunks = []

        def _collect_from_tool_results(tool_results):
            for result in (tool_results or []):
                if not isinstance(result, dict):
                    continue
                for item in result.get('content') or []:
                    if not isinstance(item, dict):
                        continue
                    url = item.get('url', '')
                    title = item.get('title', '')
                    if url or title:
                        chunks.append({'text': item.get('cited_text', ''), 'title': title, 'url': url,
                                       'source': 'web_search'})

        citations_raw = message.get('citations') or []
        if citations_raw and isinstance(citations_raw[0], dict) and 'content' in citations_raw[0]:
            # Anthropic (via litellm): list of web_search_tool_result objects, each with a
            # nested content[] of {title, url, ...} — not a flat {url, title, cited_text} dict.
            _collect_from_tool_results(citations_raw)
        else:
            for group in citations_raw:
                if not isinstance(group, list):
                    continue
                for citation in group:
                    if not isinstance(citation, dict):
                        continue
                    url = citation.get('url', '')
                    title = citation.get('title', '')
                    text = citation.get('cited_text', '')
                    if url or title:
                        chunks.append({'text': text, 'title': title, 'url': url, 'source': 'web_search'})

        if not chunks:
            # 'citations' can be null even when a web search happened — the raw provider
            # payload nests results here instead.
            web_search_results = (message.get('provider_specific_fields') or {}).get('web_search_results') or []
            _collect_from_tool_results(web_search_results)

        return chunks

    def _extract_citation_chunks_from_stream(self, citation_events):
        """Extract web search citations from stream citation events and return as chunk dicts."""
        chunks = []
        for event in (citation_events or []):
            if not isinstance(event, dict):
                continue
            # Gateway may send each citation event directly as a citation dict,
            # or as a list of citation dicts under a key
            candidates = event.get('citations') or event.get('results') or [event]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                url = item.get('url', '')
                title = item.get('title', '')
                text = item.get('cited_text', '') or item.get('text', '')
                if url or title:
                    chunks.append({'text': text, 'title': title, 'url': url, 'source': 'web_search'})
        return chunks

    def _parse_if_string(self, value, fallback):
        """Some models (e.g. Llama) return arrays/objects as JSON strings inside tool args. Parse them."""
        if not isinstance(value, str):
            return value
        try:
            import json as _json
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, type(fallback)) else fallback
        except Exception:
            return fallback

    def _with_web_search_tool(self, company_bot, params, tools, use_web_search, turn_usage):
        """T28: add web search to one call. Returns (tools, searches this call may make).

        anthropic and openrouter get a raw tool sized to what is left of the turn's budget
        (a KB turn makes several calls and `max_uses` is per request). Other providers keep
        the `web_search_options` param, only while web search is on.
        """
        provider = get_effective_provider_model(company_bot)[0]
        if provider not in RAW_WEB_SEARCH_PROVIDERS:
            if not use_web_search:
                params.pop('web_search_options', None)
            return tools, 0
        params.pop('web_search_options', None)
        if provider == 'openrouter':
            self._drop_legacy_web_plugin(params)
        if not use_web_search:
            return tools, 0
        used = turn_usage or {}
        # A call whose search count we could not read may have used any number of them: none are left.
        left = 0 if used.get('web_searches_unsure') else web_search_budget(company_bot) - used.get('web_searches', 0)
        tool = build_web_search_tool(company_bot, provider, left)
        if tool is None:
            return tools, 0
        return list(tools or []) + [tool], left

    def _drop_legacy_web_plugin(self, params):
        """T28: OpenRouter's deprecated `web` plugin would search on top of the tool, past the limit.

        Copies provider_options (it is the bot's own dict) and keeps every other plugin.
        """
        options = params.get('provider_options')
        plugins = options.get('plugins') if isinstance(options, dict) else None
        if not plugins:
            return
        kept = [p for p in plugins if not (isinstance(p, dict) and p.get('id') == 'web')]
        if len(kept) == len(plugins):
            return
        options = dict(options)
        if kept:
            options['plugins'] = kept
        else:
            options.pop('plugins')
        params['provider_options'] = options

    def _count_web_searches(self, turn_usage, searches_allowed, usage, citations, web_search_results):
        """T28: add this call's searches to the turn count (never more than it was allowed).

        Best source: the gateway's usage.web_search_requests (the provider's own count). Without
        it: Anthropic replies hold one web_search_tool_result per search, so count those. Any
        other citation shape doesn't give the count: the call is marked unsure and
        `_with_web_search_tool` offers no more searches this turn, so a retry or a second tool
        call never gets a fresh allowance. `web_searches` stays the count we know, for the log.
        T50: web_search is offered from the first call, and providers omit the count when they did
        not search. So the FIRST call of a turn with no evidence at all (no count, no search
        results, no citations) counts as no search and keeps the allowance. Any later no-evidence
        call in the same turn is marked unsure, as above. Worst case: one extra allowance per turn,
        only when the first call searched, found nothing, and the count was lost.
        `web_no_evidence_calls` is the per-turn marker for this; `_with_turn_usage` strips it.
        """
        if turn_usage is None or not searches_allowed:
            return
        reported = (usage or {}).get('web_search_requests')
        if reported is not None:
            turn_usage['web_searches'] = turn_usage.get('web_searches', 0) + min(int(reported), searches_allowed)
            return
        tool_results = [c for c in (citations or []) if isinstance(c, dict) and 'content' in c]
        if not tool_results:
            tool_results = [r for r in (web_search_results or []) if isinstance(r, dict)]
        if tool_results:
            turn_usage['web_searches'] = turn_usage.get('web_searches', 0) + min(len(tool_results), searches_allowed)
            return
        if citations:
            turn_usage['web_searches'] = turn_usage.get('web_searches', 0) + 1
            turn_usage['web_searches_unsure'] = turn_usage.get('web_searches_unsure', 0) + 1
            return
        if turn_usage.get('web_no_evidence_calls', 0) >= 1:
            turn_usage['web_searches_unsure'] = turn_usage.get('web_searches_unsure', 0) + 1
        turn_usage['web_no_evidence_calls'] = turn_usage.get('web_no_evidence_calls', 0) + 1

    def _with_turn_usage(self, response_data, extra, finish, turn_usage, request_ids=None):
        """Return a (response, extra, finish) tuple with turn_usage and the gateway request ids injected into extra."""
        # print, like the other [tool_loop] lines: logger.info is below the worker's log level.
        print(
            f"[tool_loop] web_searches={turn_usage.get('web_searches', 0)} "
            f"unsure_calls={turn_usage.get('web_searches_unsure', 0)}"
        )
        turn_usage = {k: v for k, v in turn_usage.items() if k not in ('web_searches', 'web_searches_unsure', 'web_no_evidence_calls')}
        if any(turn_usage.values()):
            extra = dict(extra) if isinstance(extra, dict) else {}
            extra['_usage_cost'] = dict(turn_usage)
        if request_ids:
            extra = dict(extra) if isinstance(extra, dict) else {}
            extra['_llm_request_ids'] = list(request_ids)
        return response_data, extra or None, finish

    def _accumulate_usage(self, accumulator, usage_cost):
        """Add a single call's usage_cost into a running accumulator dict."""
        for k in ('input_tokens', 'output_tokens', 'total_tokens', 'cache_read_tokens', 'cache_write_tokens'):
            accumulator[k] = accumulator.get(k, 0) + (usage_cost.get(k) or 0)
        accumulator['cost_usd'] = round(accumulator.get('cost_usd', 0) + (usage_cost.get('cost_usd') or 0), 6)

    def _bot_history_id(self, company_bot):
        """Newest CompanyBot history_id (the prompt version), or None (T05). One query."""
        history = getattr(company_bot, 'history', None)
        if history is None:
            return None
        try:
            return history.order_by('-history_id').values_list('history_id', flat=True).first()
        except Exception as e:
            logger.error(f'[_bot_history_id] failed: {e}')
            return None

    def _last_ai_chat_id(self, session_id):
        """Id of the newest AI CompanyChat in this session, or None."""
        from chatbot.models import CompanyChat
        try:
            return CompanyChat.objects.filter(
                session=session_id, sender__id=1
            ).order_by('-id').values_list('id', flat=True).first()
        except Exception as e:
            logger.error(f'[_last_ai_chat_id] failed: {e}')
            return None

    def _update_last_chat_usage(self, session_id, usage_cost, request_ids=None, after_chat_id=None):
        """Merge usage cost and the turn's gateway request ids into the most recently saved AI CompanyChat.

        Request ids are saved only if that message is newer than after_chat_id (saved by this turn).
        """
        from chatbot.models import CompanyChat
        try:
            chat = CompanyChat.objects.filter(
                session=session_id, sender__id=1
            ).order_by('-id').first()
            if chat:
                other_params = chat.other_params or {}
                if usage_cost:
                    other_params['usage'] = usage_cost
                if request_ids and (after_chat_id is None or chat.id > after_chat_id):
                    # Joins this chat row to its gateway ledger rows (T04).
                    other_params['llm_request_ids'] = list(request_ids)
                chat.other_params = other_params
                chat.save(update_fields=['other_params'])
                logger.info(f"[usage] saved turn usage to CompanyChat id={chat.id} usage={usage_cost}")
        except Exception as e:
            logger.error(f'[_update_last_chat_usage] failed: {e}')

    def _extract_usage_cost(self, data):
        """Extract token usage and cost from a raw gateway response dict."""
        if not data:
            return None
        usage = data.get('usage', {}) or {}
        cost = data.get('cost', {}) or {}
        result = {
            'input_tokens': usage.get('input_tokens', 0) or 0,
            'output_tokens': usage.get('output_tokens', 0) or 0,
            'total_tokens': usage.get('total_tokens', 0) or 0,
            'cache_read_tokens': usage.get('input_tokens_cache_read', 0) or 0,
            'cache_write_tokens': usage.get('input_tokens_cache_write', 0) or 0,
            'cost_usd': cost.get('computed_usd', 0) or 0,
        }
        return result if any(result.values()) else None

    def _update_session_usage(self, session_id, usage_cost):
        """Accumulate token usage and cost into ChatSession.other_params['usage']."""
        try:
            # Locked so this read-modify-write of other_params can't race with the other
            # other_params writers on this model (_save_finalized_sources,
            # _save_pending_text_conversion, async_consumer.translate_message's pop).
            with transaction.atomic():
                session = ChatSession.objects.select_for_update().get(session=session_id)
                other_params = session.other_params or {}
                usage = other_params.get('usage', {})
                usage['total_input_tokens'] = usage.get('total_input_tokens', 0) + usage_cost['input_tokens']
                usage['total_output_tokens'] = usage.get('total_output_tokens', 0) + usage_cost['output_tokens']
                usage['total_tokens'] = usage.get('total_tokens', 0) + usage_cost['total_tokens']
                usage['total_cache_read_tokens'] = (
                    usage.get('total_cache_read_tokens', 0) + (usage_cost.get('cache_read_tokens') or 0)
                )
                usage['total_cache_write_tokens'] = (
                    usage.get('total_cache_write_tokens', 0) + (usage_cost.get('cache_write_tokens') or 0)
                )
                usage['total_cost_usd'] = round(usage.get('total_cost_usd', 0) + usage_cost['cost_usd'], 6)
                other_params['usage'] = usage
                session.other_params = other_params
                session.save(update_fields=['other_params'])
            logger.info(f"[usage] session {session_id} totals updated: {usage}")
        except Exception as e:
            logger.error(f'[_update_session_usage] failed: {e}')

    def _save_finalized_sources(self, session_id, finalized_sources):
        """Persist finalized_sources from respond_to_user into ChatSession.other_params."""
        try:
            # Locked for the same reason as _update_session_usage above.
            with transaction.atomic():
                session = ChatSession.objects.select_for_update().get(session=session_id)
                other_params = session.other_params or {}
                other_params['finalized_sources'] = finalized_sources
                session.other_params = other_params
                session.save(update_fields=['other_params'])
        except Exception as e:
            logger.error(f'[_save_finalized_sources] failed: {e}')

    def _save_pending_text_conversion(self, session_id, next_reply_conversion):
        """Persist a one-shot translate/transliterate override for the user's next reply,
        from respond_to_user.next_reply_conversion. Consumed and cleared by
        async_consumer.translate_message on the next inbound message."""
        try:
            # Locked so this read-modify-write of other_params can't race with
            # async_consumer.translate_message's own locked pop of the same key —
            # without this, a concurrent unlocked write here could silently resurrect
            # an override translate_message just cleared, or a concurrent unlocked
            # pop there could drop this write before it's ever consumed.
            with transaction.atomic():
                session = ChatSession.objects.select_for_update().get(session=session_id)
                other_params = session.other_params or {}
                other_params['pending_text_conversion_type'] = next_reply_conversion
                session.other_params = other_params
                session.save(update_fields=['other_params'])
        except Exception as e:
            logger.error(f'[_save_pending_text_conversion] failed: {e}')

    def _send_chunk(self, channel_name, content, finish_reason, extra_content=None):
        """Send a chunk via channel layer to the WebSocket."""
        try:
            message_data = {
                "type": "chat.message",
                "text": {
                    "msg": content,
                    "source": "bot",
                    "type": "chunk",
                    "finish_reason": finish_reason
                },
            }

            if extra_content:
                message_data["text"]["extra_content"] = extra_content

            async_to_sync(channel_layer.send)(channel_name, message_data)

        except Exception as e:
            logger.error(f"Failed to send chunk to channel {channel_name}: {e}", exc_info=True)

    def _send_error_chunk(self, channel_name, error_msg):
        """Send error message via channel layer to the WebSocket."""
        try:
            async_to_sync(channel_layer.send)(
                channel_name,
                {
                    "type": "chat.message",
                    "text": {
                        "msg": error_msg,
                        "source": "bot",
                        "type": "error",
                        "finish_reason": "error"
                    },
                },
            )
        except Exception as e:
            logger.error(f"Failed to send error to channel {channel_name}: {e}")

    def get_default_tools_config(self):
        """Get default tools configuration - fallback for when no tool_context is available"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_state_information",
                    "description": "Get the information of the state you want to be in.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "state_name": {
                                "type": "string",
                                "description": "Name of the next state provided in the context."
                            }
                        },
                        "required": ["state_name"]
                    }
                }
            }
        ]

    def get_tools_config(self):
        """Deprecated - use get_default_tools_config() or PromptBuilder.get_tools_from_state_machine()"""
        logger.info("get_tools_config() is deprecated, use get_default_tools_config() instead")
        return self.get_default_tools_config()

    def is_function_call(self, response):
        """Check if response is a function call"""
        if isinstance(response, dict):
            if 'toolUseId' in response and 'name' in response:
                return response.get('name') == 'get_state_information'

            elif 'name' in response and 'parameters' in response:
                return response.get('name') == 'get_state_information'

            elif 'function_call' in response:
                function_call = response.get('function_call', {})
                return function_call.get('name') == 'get_state_information'

            elif 'tool_calls' in response:
                tool_calls = response.get('tool_calls', [])
                for tool_call in tool_calls:
                    if 'function' in tool_call:
                        function = tool_call.get('function', {})
                        if function.get('name') == 'get_state_information':
                            return True
                return False

            elif 'output' in response and 'message' in response.get('output', {}):
                content = response['output']['message'].get('content', [])
                for item in content:
                    if 'toolUse' in item:
                        tool_use = item.get('toolUse', {})
                        if tool_use.get('name') == 'get_state_information':
                            return True
                return False

            elif 'parameters' in response or 'input' in response:
                nested_data = response.get('parameters') or response.get('input')
                if isinstance(nested_data, dict):
                    if 'next_state_name' in nested_data:
                        return True
                    elif 'response' in nested_data:
                        return False
                    else:
                        return 'get_state_information' in str(nested_data)
                return 'get_state_information' in str(nested_data)

            elif any(key in response for key in ['toolUseId', 'tool_calls', 'function_call']):
                return 'get_state_information' in str(response)

            return False

        elif isinstance(response, str):
            return 'get_state_information' in response

        return False

    def save_message(self, session_id, profile_id, message, chunks,
                     status, translated_message, stage=None, other_params=None, bot_history_id=None):
        """Save message to database"""
        save_in_company_db(
            session_id=session_id,
            profile_id=profile_id,
            initiated_by='AI',
            message=message,
            chunks=chunks,
            status=status,
            translated_message=translated_message,
            stage=stage,
            other_params=other_params,
            bot_history_id=bot_history_id,
        )

    def translate_message(self, message, channel_name, step_number, language, company_bot, extra_content=None,
                          is_bot_vernacular_message=False):
        """Translate and send message"""
        return translate_and_send_message(
            accumulated_message=message,
            current_channel_name=channel_name,
            current_step_number=step_number,
            finish_reason="stop",
            route=language,
            company_bot=company_bot,
            extra_content=extra_content,
            is_bot_vernacular_message=is_bot_vernacular_message,
        )

    def get_chat_status(self, state_machine, company_bot):
        """Determine chat status based on state"""
        last_state = CompanyStateMachine.objects.filter(company_bot=company_bot).order_by('step').last()
        max_step = last_state.step if last_state else None

        if state_machine.step == max_step:
            return ChatStatus.COMPLETED
        else:
            return ChatStatus.IN_PROGRESS

    @abstractmethod
    def check_early_return(self, chat_session, **kwargs):
        """Check if we should return early (bot-specific logic)"""
        pass

    @abstractmethod
    def get_messages_for_llm(self, **kwargs):
        """Get appropriate messages for LLM"""
        pass

    @abstractmethod
    def process_response(self, response, chat_session, chunks, **kwargs):
        """Process the LLM response (bot-specific logic)"""
        pass
