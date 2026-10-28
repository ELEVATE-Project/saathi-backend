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
