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
