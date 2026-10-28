from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from chatbot.services.response_handlers.base_response_handler import BaseResponseHandler

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

    def test_notes_do_not_order_a_web_search(self):
        for chunks in ([], [CHUNK]):
            with self.subTest(kb_chunks=len(chunks)):
                _, tool_result = self.run_turn(chunks=chunks)
                self.assertNotIn('call the web_search tool now', tool_result)

    def test_kb_empty_web_off_says_answer_is_not_from_saathi_material(self):
        second, tool_result = self.run_turn(chunks=[], enable_web_search=False)
        self.assertNotIn('web_search_options', second['params'])
        self.assertIn("not from Saathi's material", tool_result)
