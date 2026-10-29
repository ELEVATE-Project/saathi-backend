import asyncio
import io
import json
import os
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from chatbot.models import CompanyBotTypeChoices
from chatbot.services.response_handlers.base_response_handler import BaseResponseHandler
from chatbot.services.response_handlers.common_handler import CommonResponseHandler

HANDLER_MODULE = 'chatbot.services.response_handlers.base_response_handler'

KB_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_knowledge_base',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}},
    },
}

KB_CALL = {
    'choices': [{
        'message': {
            'content': None,
            'tool_calls': [{
                'id': 'tc_1', 'type': 'function',
                'function': {'name': 'search_knowledge_base', 'arguments': '{"query": "fractions"}'},
            }],
        },
        'finish_reason': 'tool_calls',
    }],
}

FINAL_ANSWER = {
    'choices': [{'message': {'content': 'Here is an activity.'}, 'finish_reason': 'stop'}],
}

CHUNK = {'title': 'Fractions', 'url': '', 'text': 'Fold a paper strip in halves.'}


class _Handler(BaseResponseHandler):
    def check_early_return(self, chat_session, **kwargs):
        return None

    def get_messages_for_llm(self, **kwargs):
        return []

    def process_response(self, response, chat_session, chunks, **kwargs):
        return response


class WebSearchFallbackTests(SimpleTestCase):
    """T08: no prefilled assistant turn; web search stays a fallback; notes don't order a web search."""

    def run_turn(self, chunks, enable_web_search=True):
        bot = SimpleNamespace(enable_web_search=enable_web_search, stream=False, other_params={})
        gateway = mock.Mock(side_effect=[KB_CALL, FINAL_ANSWER])
        with mock.patch(f'{HANDLER_MODULE}.call_llm_gateway', gateway), \
                mock.patch(f'{HANDLER_MODULE}.build_gateway_params',
                           side_effect=lambda _bot: {'web_search_options': {'search_context_size': 'low'}}), \
                mock.patch(f'{HANDLER_MODULE}.get_effective_provider_model', return_value=('anthropic', 'm')), \
                mock.patch('chatbot.services.vector.vector_service.fetch_context_for_query',
                           return_value=('', chunks)):
            result = _Handler()._handle_gateway_response(
                system_prompt='You are Saathi.', messages=[{'role': 'user', 'content': 'A fractions activity?'}],
                company_bot=bot, session_id='s1', profile_id=None, tools=[KB_TOOL],
            )
        self.assertEqual(result[0], 'Here is an activity.')
        self.assertEqual(gateway.call_count, 2)
        second = gateway.call_args_list[1].kwargs
        tool_result = next(m for m in second['messages'] if m['role'] == 'tool')['content']
        return second, tool_result

    def test_kb_empty_does_not_end_with_assistant_message(self):
        second, _ = self.run_turn(chunks=[])
        self.assertNotEqual(second['messages'][-1]['role'], 'assistant')
        self.assertIn('web_search_options', second['params'])

    def test_kb_hit_keeps_web_search_as_fallback(self):
        second, _ = self.run_turn(chunks=[CHUNK])
        self.assertNotEqual(second['messages'][-1]['role'], 'assistant')
        self.assertIn('web_search_options', second['params'])

    def test_kb_hit_note_offers_web_only_as_fallback(self):
        _, tool_result = self.run_turn(chunks=[CHUNK])
        self.assertNotIn('call the web_search tool now', tool_result)
        self.assertIn('web search is available as a fallback', tool_result)

    def test_kb_empty_note_gives_h06_order(self):
        # H06: KB first, then web, then general knowledge with a note.
        _, tool_result = self.run_turn(chunks=[])
        self.assertNotIn('call the web_search tool now', tool_result)
        self.assertIn('Use web search to answer it', tool_result)
        self.assertIn("not from Saathi's material or the web", tool_result)

    def test_kb_empty_web_off_says_answer_is_not_from_saathi_material(self):
        second, tool_result = self.run_turn(chunks=[], enable_web_search=False)
        self.assertNotIn('web_search_options', second['params'])
        self.assertIn("not from Saathi's material", tool_result)


def _gateway_patches(gateway=None, stream=None):
    """Patch the gateway and its helpers in the base handler module; returns a list of patchers."""
    patches = [
        mock.patch(f'{HANDLER_MODULE}.build_gateway_params', side_effect=lambda _bot: {}),
        mock.patch(f'{HANDLER_MODULE}.get_effective_provider_model', return_value=('anthropic', 'm')),
        mock.patch(f'{HANDLER_MODULE}.save_in_company_db'),
        mock.patch.object(BaseResponseHandler, '_send_chunk'),
        mock.patch.object(BaseResponseHandler, '_save_finalized_sources'),
    ]
    if gateway is not None:
        patches.append(mock.patch(f'{HANDLER_MODULE}.call_llm_gateway', gateway))
    if stream is not None:
        patches.append(mock.patch(f'{HANDLER_MODULE}.call_llm_gateway_stream', stream))
    return patches


def _call_turn(bot, patches, channel_name=None):
    for p in patches:
        p.start()
    try:
        return _Handler()._handle_gateway_response(
            system_prompt='You are Saathi.', messages=[{'role': 'user', 'content': 'Hi'}],
            company_bot=bot, session_id='s1', profile_id=None, tools=None, channel_name=channel_name,
        )
    finally:
        for p in reversed(patches):
            p.stop()


class ShortReplyTests(SimpleTestCase):
    """T07: a short but valid reply is sent as it is, without a retry."""

    def test_one_word_reply_is_sent_as_is(self):
        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                handler = CommonResponseHandler()
                bot = SimpleNamespace(bot_type=CompanyBotTypeChoices.SIMPLE, stream=streamed)
                with mock.patch.object(handler, 'get_llm_response') as llm, \
                        mock.patch.object(handler, '_handle_regular_response', return_value='sent') as send:
                    result = handler.process_response(
                        'Thanks!', SimpleNamespace(current_step=1), [], streaming_completed=streamed,
                        company_bot=bot, language='en', messages=[],
                    )
                self.assertEqual(result, 'sent')
                llm.assert_not_called()
                self.assertEqual(send.call_args.kwargs['response'], 'Thanks!')


class TruncatedReplyTests(SimpleTestCase):
    """T07: a reply cut off by the token limit is logged, sent as it is, and not retried."""

    def test_non_stream_cut_off_reply_is_logged_not_retried(self):
        for reason in ('length', 'max_tokens'):
            with self.subTest(reason=reason):
                gateway = mock.Mock(return_value={
                    'choices': [{'message': {'content': 'Step 1: fold the'}, 'finish_reason': reason}],
                })
                bot = SimpleNamespace(enable_web_search=False, stream=False, other_params={})
                with self.assertLogs('django', level='WARNING') as logs:
                    result = _call_turn(bot, _gateway_patches(gateway=gateway))
                self.assertEqual(result[0], 'Step 1: fold the')
                self.assertEqual(gateway.call_count, 1)
                self.assertTrue(any('truncated' in line for line in logs.output))

    def test_stream_cut_off_reply_is_logged_not_retried(self):
        stream = mock.Mock(return_value=iter([
            ('Step 1: fold the', None, None, None, None),
            (None, None, 'length', None, None),
        ]))
        bot = SimpleNamespace(enable_web_search=False, stream=True, other_params={})
        with self.assertLogs('django', level='WARNING') as logs:
            result = _call_turn(bot, _gateway_patches(stream=stream), channel_name='c1')
        self.assertEqual(result[0], 'Step 1: fold the')
        self.assertEqual(stream.call_count, 1)
        self.assertTrue(any('truncated' in line for line in logs.output))


class EmptyRespondToUserTests(SimpleTestCase):
    """T07 keeps this: respond_to_user with no text is a real failure and gets one retry."""

    def test_empty_respond_to_user_retries_once(self):
        def reply(text):
            return {'choices': [{
                'message': {'content': text, 'tool_calls': [{
                    'id': 'tc_1', 'type': 'function',
                    'function': {'name': 'respond_to_user', 'arguments': '{"quick_reply_chips": []}'},
                }]},
                'finish_reason': 'tool_calls',
            }]}

        gateway = mock.Mock(side_effect=[reply(''), reply('Hello, how can I help?')])
        bot = SimpleNamespace(enable_web_search=False, stream=False, other_params={})
        result = _call_turn(bot, _gateway_patches(gateway=gateway))
        self.assertEqual(result[0], 'Hello, how can I help?')
        self.assertEqual(gateway.call_count, 2)


THINKING = [
    {'type': 'thinking', 'thinking': 'The teacher wants an activity.', 'signature': 'sig-1'},
    {'type': 'redacted_thinking', 'data': 'opaque'},
]
HAIKU_55_SETTINGS = {'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'low'}}


def _bot(**overrides):
    fields = dict(
        max_token=8192, bot_temperature=0.3, connect_timeout=None, read_timeout=None,
        other_params={}, gateway_provider='anthropic', gateway_sub_provider=None,
        gateway_model='claude-haiku-4-5', enable_web_search=False, enable_cache=False,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class Haiku55ParamsTests(SimpleTestCase):
    """T31: a Haiku 5.5 bot sends thinking and effort from other_params, and no temperature."""

    def test_haiku_55_sends_thinking_and_effort_without_temperature(self):
        from chatbot.llm_models.llm_gateway import build_gateway_params
        cases = {
            'gateway_model': _bot(gateway_model='claude-haiku-5-5', other_params=dict(HAIKU_55_SETTINGS)),
            'openrouter slug': _bot(gateway_provider='openrouter', gateway_model='anthropic/claude-haiku-5.5',
                                    other_params=dict(HAIKU_55_SETTINGS)),
            'custom_model': _bot(other_params={**HAIKU_55_SETTINGS, 'custom_model': 'claude-haiku-5-5'}),
        }
        for name, bot in cases.items():
            with self.subTest(name):
                with self.assertLogs('django', level='WARNING') as logs:
                    params = build_gateway_params(bot)
                self.assertNotIn('temperature', params)
                self.assertEqual(params['thinking'], {'type': 'adaptive'})
                self.assertEqual(params['output_config'], {'effort': 'low'})
                self.assertTrue(any('temperature' in line for line in logs.output))

    def test_haiku_45_request_is_unchanged(self):
        from chatbot.llm_models.llm_gateway import build_gateway_params
        params = build_gateway_params(_bot(other_params={'seed': 7}))
        self.assertEqual(params, {'max_tokens': 8192, 'temperature': 0.3, 'seed': 7})


def _kb_call(thinking_blocks=None):
    message = {
        'content': None,
        'tool_calls': [{
            'id': 'toolu_1', 'type': 'function',
            'function': {'name': 'search_knowledge_base', 'arguments': '{"query": "fractions"}'},
        }],
    }
    if thinking_blocks is not None:
        message['thinking_blocks'] = thinking_blocks
    return {'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}


def _kb_stream(thinking_blocks=None):
    finish = {'finish_reason': 'tool_calls'}
    if thinking_blocks is not None:
        finish['thinking_blocks'] = thinking_blocks
    return iter([
        ('', {'index': 0, 'id': 'toolu_1', 'name': 'search_knowledge_base',
              'arguments_delta': '{"query": "fractions"}'}, None, None, None),
        ('', None, 'tool_calls', None, finish),
    ])


def _answer_stream():
    return iter([
        ('Here is an activity.', None, None, None, None),
        ('', None, 'stop', None, {'finish_reason': 'stop'}),
    ])


def _kb_turn(bot, gateway=None, stream=None, channel_name=None):
    patches = _gateway_patches(gateway=gateway, stream=stream) + [
        mock.patch('chatbot.services.vector.vector_service.fetch_context_for_query', return_value=('', [CHUNK])),
    ]
    for p in patches:
        p.start()
    try:
        return _Handler()._handle_gateway_response(
            system_prompt='You are Saathi.', messages=[{'role': 'user', 'content': 'A fractions activity?'}],
            company_bot=bot, session_id='s1', profile_id=None, tools=[KB_TOOL], channel_name=channel_name,
        )
    finally:
        for p in reversed(patches):
            p.stop()


class ThinkingToolLoopTests(SimpleTestCase):
    """T31: the assistant tool call re-sent after search_knowledge_base keeps its thinking blocks."""

    def check_second_call(self, second_messages, expect_blocks):
        self.assertEqual(second_messages[-1]['role'], 'tool')
        self.assertEqual(second_messages[-1]['tool_call_id'], 'toolu_1')
        assistant = second_messages[-2]
        self.assertEqual(assistant['role'], 'assistant')
        self.assertEqual(assistant['tool_calls'][0]['id'], 'toolu_1')
        if expect_blocks:
            self.assertEqual(assistant['thinking_blocks'], THINKING)
        else:
            self.assertNotIn('thinking_blocks', assistant)

    def test_non_stream_thinking_blocks_are_resent_in_order(self):
        gateway = mock.Mock(side_effect=[_kb_call(THINKING), FINAL_ANSWER])
        bot = SimpleNamespace(enable_web_search=False, stream=False, other_params=dict(HAIKU_55_SETTINGS))
        result = _kb_turn(bot, gateway=gateway)
        self.assertEqual(result[0], 'Here is an activity.')
        self.check_second_call(gateway.call_args_list[1].kwargs['messages'], expect_blocks=True)

    def test_stream_thinking_blocks_are_resent_in_order(self):
        stream = mock.Mock(side_effect=[_kb_stream(THINKING), _answer_stream()])
        bot = SimpleNamespace(enable_web_search=False, stream=True, other_params=dict(HAIKU_55_SETTINGS))
        result = _kb_turn(bot, stream=stream, channel_name='c1')
        self.assertEqual(result[0], 'Here is an activity.')
        self.check_second_call(stream.call_args_list[1].kwargs['messages'], expect_blocks=True)

    def test_haiku_45_tool_call_has_no_thinking_blocks(self):
        gateway = mock.Mock(side_effect=[_kb_call(), FINAL_ANSWER])
        bot = SimpleNamespace(enable_web_search=False, stream=False, other_params={})
        _kb_turn(bot, gateway=gateway)
        self.check_second_call(gateway.call_args_list[1].kwargs['messages'], expect_blocks=False)

    def test_thinking_on_but_no_blocks_is_logged(self):
        gateway = mock.Mock(side_effect=[_kb_call(), FINAL_ANSWER])
        bot = SimpleNamespace(enable_web_search=False, stream=False, other_params=dict(HAIKU_55_SETTINGS))
        with self.assertLogs('django', level='WARNING') as logs:
            _kb_turn(bot, gateway=gateway)
        self.assertTrue(any('no thinking blocks' in line for line in logs.output))

    def test_empty_respond_to_user_retry_keeps_thinking_blocks(self):
        def reply(text, blocks=None):
            message = {'content': text, 'tool_calls': [{
                'id': 'tc_1', 'type': 'function',
                'function': {'name': 'respond_to_user', 'arguments': '{"quick_reply_chips": []}'},
            }]}
            if blocks is not None:
                message['thinking_blocks'] = blocks
            return {'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}

        gateway = mock.Mock(side_effect=[reply('', THINKING), reply('Hello, how can I help?')])
        bot = SimpleNamespace(enable_web_search=False, stream=False, other_params=dict(HAIKU_55_SETTINGS))
        result = _call_turn(bot, _gateway_patches(gateway=gateway))
        self.assertEqual(result[0], 'Hello, how can I help?')
        retry_assistant = gateway.call_args_list[1].kwargs['messages'][-2]
        self.assertEqual(retry_assistant['role'], 'assistant')
        self.assertEqual(retry_assistant['thinking_blocks'], THINKING)


class RefusalTests(SimpleTestCase):
    """T31: a safety refusal gets the bot's error message, with no web-search or other retry."""

    def setUp(self):
        patcher = mock.patch.object(BaseResponseHandler, 'get_error_message', return_value=('Maaf kijiye.', True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_stream_refusal_is_not_retried(self):
        for reason in ('content_filter', 'refusal'):
            with self.subTest(reason=reason):
                gateway = mock.Mock(return_value={
                    'choices': [{'message': {'content': ''}, 'finish_reason': reason}],
                })
                bot = SimpleNamespace(enable_web_search=True, stream=False, other_params={})
                with self.assertLogs('django', level='WARNING') as logs:
                    result = _kb_turn(bot, gateway=gateway)
                self.assertEqual(result, ('Maaf kijiye.', {'_is_vernacular_error': True}, None))
                self.assertEqual(gateway.call_count, 1)
                self.assertTrue(any('refusal' in line for line in logs.output))

    def test_refusal_keeps_turn_usage(self):
        gateway = mock.Mock(side_effect=[
            {**_kb_call(), 'usage': {'input_tokens': 100, 'output_tokens': 5, 'total_tokens': 105}},
            {'choices': [{'message': {'content': ''}, 'finish_reason': 'content_filter'}],
             'usage': {'input_tokens': 150, 'output_tokens': 12, 'total_tokens': 162}},
        ])
        bot = SimpleNamespace(enable_web_search=True, stream=False, other_params={})
        with mock.patch.object(BaseResponseHandler, '_update_session_usage'):
            result = _kb_turn(bot, gateway=gateway)
        self.assertEqual(result[0], 'Maaf kijiye.')
        self.assertTrue(result[1]['_is_vernacular_error'])
        self.assertEqual(result[1]['_usage_cost']['input_tokens'], 250)

    def test_stream_refusal_after_kb_preamble_closes_the_bubble(self):
        refusal = iter([('', None, 'content_filter', None, {'finish_reason': 'content_filter'})])
        preamble_kb = iter([('Let me check the material. ', None, None, None, None)] + list(_kb_stream(THINKING)))
        stream = mock.Mock(side_effect=[preamble_kb, refusal])
        bot = SimpleNamespace(enable_web_search=False, stream=True, other_params=dict(HAIKU_55_SETTINGS))
        patches = _gateway_patches(stream=stream) + [
            mock.patch('chatbot.services.vector.vector_service.fetch_context_for_query', return_value=('', [CHUNK])),
        ]
        for p in patches:
            p.start()
        try:
            result = _Handler()._handle_gateway_response(
                system_prompt='You are Saathi.', messages=[{'role': 'user', 'content': 'Hi'}],
                company_bot=bot, session_id='s1', profile_id=None, tools=[KB_TOOL], channel_name='c1',
            )
            sent = [c.kwargs.get('finish_reason') for c in BaseResponseHandler._send_chunk.call_args_list]
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(result[0], 'Maaf kijiye.')
        self.assertEqual(sent[-1], 'stop')

    def test_stream_refusal_is_not_retried_or_saved(self):
        stream = mock.Mock(return_value=iter([
            ('I', None, None, None, None),
            ('', None, 'content_filter', None, {'finish_reason': 'content_filter'}),
        ]))
        bot = SimpleNamespace(enable_web_search=True, stream=True, other_params={})
        with mock.patch(f'{HANDLER_MODULE}.save_in_company_db') as save, \
                self.assertLogs('django', level='WARNING') as logs:
            patches = [p for p in _gateway_patches(stream=stream) if 'save_in_company_db' not in str(p.attribute)]
            for p in patches:
                p.start()
            try:
                result = _Handler()._handle_gateway_response(
                    system_prompt='You are Saathi.', messages=[{'role': 'user', 'content': 'Hi'}],
                    company_bot=bot, session_id='s1', profile_id=None, tools=[KB_TOOL], channel_name='c1',
                )
                sent = [c.kwargs.get('finish_reason') for c in BaseResponseHandler._send_chunk.call_args_list]
            finally:
                for p in reversed(patches):
                    p.stop()
        self.assertEqual(result, ('Maaf kijiye.', {'_is_vernacular_error': True}, None))
        self.assertEqual(stream.call_count, 1)
        save.assert_not_called()
        # The refused text already shown is closed as its own bubble, so the error message isn't glued to it.
        self.assertEqual(sent[-1], 'stop')
        self.assertTrue(any('refusal' in line for line in logs.output))


class ThinkingNotSentToClientTests(SimpleTestCase):
    """T31 review: thinking blocks never reach the WebSocket client."""

    def test_streamed_reply_stop_chunk_has_no_thinking_blocks(self):
        stream = mock.Mock(return_value=iter([
            ('Hello, how can I help?', None, None, None, None),
            ('', {'index': 0, 'id': 'tc_1', 'name': 'respond_to_user',
                  'arguments_delta': '{"quick_reply_chips": ["Yes"]}'}, None, None, None),
            ('', None, 'tool_calls', None, {'finish_reason': 'tool_calls', 'thinking_blocks': THINKING}),
        ]))
        bot = SimpleNamespace(enable_web_search=False, stream=True, other_params=dict(HAIKU_55_SETTINGS))
        patches = _gateway_patches(stream=stream)
        for p in patches:
            p.start()
        try:
            result = _Handler()._handle_gateway_response(
                system_prompt='You are Saathi.', messages=[{'role': 'user', 'content': 'Hi'}],
                company_bot=bot, session_id='s1', profile_id=None, tools=None, channel_name='c1',
            )
            sent_extras = [c.kwargs.get('extra_content') or {} for c in BaseResponseHandler._send_chunk.call_args_list]
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(result[0], 'Hello, how can I help?')
        self.assertEqual(sent_extras[-1].get('quick_reply_chips'), ['Yes'])
        for extra in sent_extras:
            self.assertNotIn('_thinking_blocks', extra)
        self.assertNotIn('_thinking_blocks', result[1] or {})


class LogHygieneTests(SimpleTestCase):
    """T35: secrets never reach the logs or stdout; other details stay (PD-53)."""

    TOKEN = 'eyJ-test-access-token-T35'

    def test_login_does_not_print_password(self):
        from rest_framework.test import APIRequestFactory
        from chatbot.views import api_views

        request = APIRequestFactory().post(
            '/login', {'email': 'teacher@example.com', 'password': 'hunter2-T35'}, format='json')
        out = io.StringIO()
        with mock.patch.object(api_views.Profile.objects, 'filter', return_value=[]), redirect_stdout(out):
            api_views.login(request)
        self.assertNotIn('hunter2-T35', out.getvalue())
        self.assertIn('teacher@example.com', out.getvalue())

    def test_create_profile_does_not_print_access_token(self):
        from rest_framework.test import APIRequestFactory
        from chatbot.views import profile_views

        request = APIRequestFactory().post('/profile', {'access_token': self.TOKEN}, format='json')
        out = io.StringIO()
        with mock.patch.object(profile_views, 'create_profile_utils', return_value={'success': True, 'data': {}}), \
                redirect_stdout(out):
            profile_views.create_profile_views(request)
        self.assertNotIn(self.TOKEN, out.getvalue())

    def test_openai_stt_does_not_print_api_key(self):
        from chatbot.translate.openai import openai_stt

        out = io.StringIO()
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test-key-T35'}), \
                mock.patch.object(openai_stt, 'OpenAI', side_effect=RuntimeError('stop')), \
                self.assertLogs('django', 'ERROR') as logs, redirect_stdout(out):
            openai_stt.transcribe_audio('', 'wav', 'en', SimpleNamespace(other_params={}))
        self.assertNotIn('sk-test-key-T35', out.getvalue() + '\n'.join(logs.output))

    def test_bedrock_client_does_not_print_aws_key(self):
        from chatbot.llm_models import llm_script

        client = mock.Mock()
        client.converse.side_effect = RuntimeError('stop')
        out = io.StringIO()
        with mock.patch.object(llm_script.boto3, 'client', return_value=client), redirect_stdout(out):
            try:
                llm_script.handle_bedrock_model.__wrapped__(
                    {}, messages=None, aws_key='AKIATESTKEYT35', aws_secret_key='secret-T35')
            except Exception:
                pass
        self.assertNotIn('AKIATESTKEYT35', out.getvalue())
        self.assertNotIn('secret-T35', out.getvalue())

    def _receive(self, frame):
        from chatbot.consumers import async_consumer

        consumer = async_consumer.AsyncSocketConsumer()
        consumer.handle_access_token = mock.AsyncMock(return_value='u1')
        consumer.upsert_elevate_profile = mock.AsyncMock(return_value={'error': 'unauthorized'})
        consumer.send = mock.AsyncMock()
        consumer.close = mock.AsyncMock()
        with mock.patch.object(async_consumer, 'fetch_elevate_user', return_value={}), \
                self.assertLogs('django', 'INFO') as logs:
            asyncio.run(consumer.receive(frame))
        return '\n'.join(logs.output)

    def test_authenticate_frame_log_hides_access_token_only(self):
        logged = self._receive(json.dumps({
            'type': 'authenticate', 'sessionid': 'sess-T35', 'route': '/saathi-bot-eval',
            'access_token': self.TOKEN,
        }))
        self.assertNotIn(self.TOKEN, logged)
        self.assertIn('sess-T35', logged)
        self.assertIn('/saathi-bot-eval', logged)

    def test_non_json_frame_is_logged_by_size(self):
        logged = self._receive('not json ' + self.TOKEN)
        self.assertNotIn(self.TOKEN, logged)
        self.assertIn(str(len('not json ' + self.TOKEN)), logged)


ALLOWED_METADATA_KEYS = {'session_id', 'bot_id', 'bot_history_id', 'turn_id', 'call_kind', 'language'}


def _respond(text, request_id=None):
    reply = {'choices': [{
        'message': {'content': text, 'tool_calls': [{
            'id': 'tc_1', 'type': 'function',
            'function': {'name': 'respond_to_user', 'arguments': '{"quick_reply_chips": []}'},
        }]},
        'finish_reason': 'tool_calls',
    }]}
    if request_id:
        reply['id'] = request_id
    return reply


class CallMetadataTests(SimpleTestCase):
    """T04: every LLM call carries ids (never text) so its gateway ledger row joins to the chat."""

    def turn(self, gateway=None, stream=None, tools=None, other_params=None):
        bot = SimpleNamespace(id=7, enable_web_search=False, stream=stream is not None,
                              other_params=other_params or {})
        patches = _gateway_patches(gateway=gateway, stream=stream) + [
            mock.patch.object(_Handler, '_execute_tool', return_value=('Fold a strip.', [])),
        ]
        for p in patches:
            p.start()
        try:
            return _Handler()._handle_gateway_response(
                system_prompt='You are Saathi.', messages=[{'role': 'user', 'content': 'A fractions activity?'}],
                company_bot=bot, session_id='s1', profile_id=None, tools=tools,
                channel_name='c1' if stream is not None else None, language='hi',
            )
        finally:
            for p in reversed(patches):
                p.stop()

    def sent(self, call_mock):
        return [c.kwargs['metadata'] for c in call_mock.call_args_list]

    def test_answer_call_sends_ids_only(self):
        gateway = mock.Mock(return_value=_respond('Here is an activity.', 'req-a'))
        result = self.turn(gateway=gateway)
        [metadata] = self.sent(gateway)
        self.assertLessEqual(set(metadata), ALLOWED_METADATA_KEYS)
        self.assertEqual(metadata['call_kind'], 'answer')
        self.assertEqual(metadata['session_id'], 's1')
        self.assertEqual(metadata['bot_id'], 7)
        self.assertEqual(metadata['language'], 'hi')
        self.assertTrue(metadata['turn_id'])
        self.assertNotIn('fractions', json.dumps(metadata))
        self.assertEqual(result[1]['_llm_request_ids'], ['req-a'])

    def test_call_after_tool_result_is_tool(self):
        gateway = mock.Mock(side_effect=[dict(KB_CALL, id='req-a'), dict(FINAL_ANSWER, id='req-b')])
        result = self.turn(gateway=gateway, tools=[KB_TOOL])
        kinds = [m['call_kind'] for m in self.sent(gateway)]
        self.assertEqual(kinds, ['answer', 'tool'])
        self.assertEqual(len({m['turn_id'] for m in self.sent(gateway)}), 1)
        self.assertEqual(result[1]['_llm_request_ids'], ['req-a', 'req-b'])

    def test_empty_respond_to_user_retry_is_retry(self):
        gateway = mock.Mock(side_effect=[_respond(''), _respond('Hello, how can I help?')])
        self.turn(gateway=gateway)
        self.assertEqual([m['call_kind'] for m in self.sent(gateway)], ['answer', 'retry'])

    def test_empty_retry_failure_keeps_request_ids(self):
        gateway = mock.Mock(side_effect=[_respond('', 'req-a'), _respond('', 'req-b')])
        with mock.patch.object(_Handler, 'get_error_message', return_value=('Sorry.', False)):
            result = self.turn(gateway=gateway)
        self.assertEqual(result[0], 'Sorry.')
        self.assertEqual(result[1]['_llm_request_ids'], ['req-a', 'req-b'])

    def test_failed_follow_up_call_keeps_request_ids(self):
        gateway = mock.Mock(side_effect=[dict(KB_CALL, id='req-a'), None])
        result = self.turn(gateway=gateway, tools=[KB_TOOL])
        self.assertIsNone(result[0])
        self.assertEqual(result[1], {'_llm_request_ids': ['req-a']})

    def test_request_ids_skip_a_message_from_an_earlier_turn(self):
        chat = SimpleNamespace(id=3, other_params={}, save=mock.Mock())
        with mock.patch('chatbot.models.CompanyChat.objects') as chats:
            chats.filter.return_value.order_by.return_value.first.return_value = chat
            _Handler()._update_last_chat_usage('s1', {'input_tokens': 5}, request_ids=['req-a'], after_chat_id=3)
        self.assertNotIn('llm_request_ids', chat.other_params)
        self.assertEqual(chat.other_params['usage'], {'input_tokens': 5})

    def test_stream_merges_admin_metadata_under_ours(self):
        stream = mock.Mock(return_value=iter([
            ('Hello.', None, None, None, None),
            ('', None, 'stop', None, {'finish_reason': 'stop', 'id': 'req-s'}),
        ]))
        result = self.turn(stream=stream, other_params={'metadata': {'team': 'saathi', 'call_kind': 'admin'}})
        [metadata] = self.sent(stream)
        self.assertEqual(metadata['team'], 'saathi')
        self.assertEqual(metadata['call_kind'], 'answer')
        self.assertEqual(metadata['session_id'], 's1')
        self.assertEqual(result[1]['_llm_request_ids'], ['req-s'])

    def test_title_call_is_title(self):
        from chatbot.celery_tasks import title_tasks

        bot = SimpleNamespace(id=9, tool_context=None, context='Make a title.')
        session = SimpleNamespace(title=None, company_bot=bot)
        gateway = mock.Mock(return_value=None)
        with mock.patch.object(title_tasks.ChatSession, 'objects') as sessions, \
                mock.patch.object(title_tasks.Flow, 'objects') as flows, \
                mock.patch.object(title_tasks.CompanyChat, 'objects'), \
                mock.patch.object(title_tasks, 'get_guided_chat', return_value=[]), \
                mock.patch.object(title_tasks, 'get_effective_provider_model', return_value=('anthropic', 'm')), \
                mock.patch.object(title_tasks, 'build_gateway_params', return_value={}), \
                mock.patch.object(title_tasks, 'call_llm_gateway', gateway):
            sessions.filter.return_value.first.return_value = session
            flows.filter.return_value.first.return_value = SimpleNamespace(title_bot=bot)
            title_tasks.generate_session_title('s1')
        metadata = gateway.call_args.kwargs['metadata']
        self.assertEqual(metadata['call_kind'], 'title')
        self.assertEqual(metadata['session_id'], 's1')
        self.assertEqual(metadata['bot_id'], 9)

    def test_request_ids_are_saved_on_the_chat_row(self):
        chat = SimpleNamespace(id=3, other_params={'usage': {'input_tokens': 1}}, save=mock.Mock())
        with mock.patch('chatbot.models.CompanyChat.objects') as chats:
            chats.filter.return_value.order_by.return_value.first.return_value = chat
            _Handler()._update_last_chat_usage('s1', {'input_tokens': 5}, request_ids=['req-a', 'req-b'])
        self.assertEqual(chat.other_params['llm_request_ids'], ['req-a', 'req-b'])
        self.assertEqual(chat.other_params['usage'], {'input_tokens': 5})


class PromptVersionTests(SimpleTestCase):
    """T05: each saved bot turn and each LLM call carry the bot's prompt version (its newest history_id)."""

    def bot_with_history(self, history_id=412, stream=False):
        history = mock.Mock()
        history.order_by.return_value.values_list.return_value.first.return_value = history_id
        return SimpleNamespace(id=7, enable_web_search=False, stream=stream, other_params={}, history=history)

    def test_history_id_is_the_newest_history_row(self):
        bot = self.bot_with_history(412)
        self.assertEqual(_Handler()._bot_history_id(bot), 412)
        bot.history.order_by.assert_called_once_with('-history_id')

    def test_bot_without_history_gives_none(self):
        self.assertIsNone(_Handler()._bot_history_id(SimpleNamespace(id=7)))
        self.assertIsNone(_Handler()._bot_history_id(None))

    def test_turn_reads_history_id_once_into_kwargs(self):
        bot = self.bot_with_history(412)
        handler = _Handler()
        with mock.patch(f'{HANDLER_MODULE}.ChatSession.objects'), \
                mock.patch.object(handler, 'check_early_return', return_value='early') as early:
            handler.handle_response(session_id='s1', company_bot=bot)
        self.assertEqual(early.call_args.kwargs['bot_history_id'], 412)
        bot.history.order_by.assert_called_once()

    def test_llm_response_passes_history_id_to_the_gateway_turn(self):
        handler = _Handler()
        with mock.patch(f'{HANDLER_MODULE}.ChatSession.objects'), \
                mock.patch(f'{HANDLER_MODULE}.CompanyStateMachine.objects'), \
                mock.patch.object(handler, '_handle_gateway_response', return_value=('Hi', None, 'stop')) as turn:
            handler.get_llm_response(
                system_prompt='You are Saathi.', messages=[], company_bot=SimpleNamespace(
                    bot_type=CompanyBotTypeChoices.SIMPLE, tool_context=None),
                session_id='s1', profile_id=None, channel_name=None, bot_history_id=412,
            )
        self.assertEqual(turn.call_args.kwargs['bot_history_id'], 412)

    def run_turn(self, gateway=None, stream=None):
        bot = self.bot_with_history(stream=stream is not None)
        patches = _gateway_patches(gateway=gateway, stream=stream)
        for p in patches:
            p.start()
        try:
            from chatbot.services.response_handlers import base_response_handler as bh
            _Handler()._handle_gateway_response(
                system_prompt='You are Saathi.', messages=[{'role': 'user', 'content': 'Hi'}],
                company_bot=bot, session_id='s1', profile_id=None, tools=None,
                channel_name='c1' if stream is not None else None, bot_history_id=412,
            )
            return bh.save_in_company_db
        finally:
            for p in reversed(patches):
                p.stop()

    def test_non_stream_call_metadata_has_history_id(self):
        gateway = mock.Mock(return_value=FINAL_ANSWER)
        self.run_turn(gateway=gateway)
        self.assertEqual(gateway.call_args.kwargs['metadata']['bot_history_id'], 412)

    def test_stream_saves_and_sends_history_id(self):
        stream = mock.Mock(return_value=iter([
            ('Hello.', None, None, None, None),
            ('', None, 'stop', None, {'finish_reason': 'stop', 'id': 'req-s'}),
        ]))
        save = self.run_turn(stream=stream)
        self.assertEqual(stream.call_args.kwargs['metadata']['bot_history_id'], 412)
        self.assertEqual(save.call_args.kwargs['bot_history_id'], 412)

    def test_regular_reply_is_saved_with_history_id(self):
        handler = CommonResponseHandler()
        bot = SimpleNamespace(bot_type=CompanyBotTypeChoices.SIMPLE)
        with mock.patch('chatbot.services.response_handlers.common_handler.CompanyStateMachine.objects'), \
                mock.patch.object(handler, 'translate_message', return_value='Hi'), \
                mock.patch(f'{HANDLER_MODULE}.save_in_company_db') as save:
            handler._handle_regular_response(
                response='Hi', chat_session=SimpleNamespace(current_step=1), company_bot=bot,
                session_id='s1', channel_name='c1', language='en', profile_id=None, chunks=[],
                current_step=1, bot_history_id=412,
            )
        self.assertEqual(save.call_args.kwargs['bot_history_id'], 412)

    def test_save_in_company_db_writes_history_id(self):
        from chatbot.celery_tasks import common_chat_tasks

        for given, stored in ((412, 412), (None, None)):
            with self.subTest(given=given), \
                    mock.patch.object(common_chat_tasks, 'Profile'), \
                    mock.patch.object(common_chat_tasks.CompanyChat, 'objects') as chats:
                kwargs = {'bot_history_id': given} if given is not None else {}
                common_chat_tasks.save_in_company_db(
                    session_id='s1', profile_id=None, initiated_by='AI', message='Hi', chunks=None,
                    status=None, **kwargs,
                )
            self.assertEqual(chats.create.call_args.kwargs['bot_history_id'], stored)

    def test_chat_column_is_nullable(self):
        from chatbot.models import CompanyChat

        self.assertTrue(CompanyChat._meta.get_field('bot_history_id').null)
