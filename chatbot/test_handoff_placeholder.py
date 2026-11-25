"""T34: translate saved hand-off text without changing the live message."""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from chatbot.models import CompanyBotTypeChoices, Voice, VoiceType
from chatbot.services.response_handlers import common_handler


class HandoffPlaceholderTests(SimpleTestCase):
    def setUp(self):
        self.handler = common_handler.CommonResponseHandler()
        self.bot = SimpleNamespace(bot_type=CompanyBotTypeChoices.SIMPLE, tag_context=None)
        self.voice = Voice(pk=34, provider='google')
        cache = getattr(common_handler, '_translate_handoff_placeholder', None)
        if cache:
            cache.cache_clear()
            self.addCleanup(cache.cache_clear)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(common_handler.CompanyStateMachine, 'objects'))
        self.voices = stack.enter_context(mock.patch.object(common_handler.Voice, 'objects'))
        self.voices.filter.return_value.first.return_value = self.voice
        self.translate = mock.Mock(return_value={'status': 200, 'content': 'समझ गया।'})
        stack.enter_context(mock.patch.object(common_handler, 'text_translate_provider', self.translate))
        stack.enter_context(mock.patch(
            'chatbot.celery_tasks.handle_message.text_translate_provider', self.translate,
        ))
        self.send = stack.enter_context(mock.patch(
            'chatbot.celery_tasks.handle_message.channel_layer.send', new_callable=mock.AsyncMock,
        ))
        self.save = stack.enter_context(mock.patch.object(self.handler, 'save_message'))

    def run_turn(self, language='hi', query='', message='', **kwargs):
        response = {'message': message, 'query': query, 'should_move_forward': 'yes'}
        result = self.handler._handle_regular_response(
            response=response, chat_session=SimpleNamespace(current_step=1), company_bot=self.bot,
            session_id='s1', channel_name='c1', language=language, profile_id=None,
            chunks=[], current_step=1, bot_history_id=412, **kwargs,
        )
        return result, self.save.call_args.kwargs if self.save.call_args else None

    def test_hindi_placeholder_saved_but_live_message_stays_empty(self):
        result, saved = self.run_turn()
        self.assertEqual(result, '')
        self.assertEqual(saved['message'], 'Understood.')
        self.assertEqual(saved['translated_message'], 'समझ गया।')
        self.assertEqual(saved['bot_history_id'], 412)
        self.voices.filter.assert_called_once_with(
            company_bot=self.bot, type=VoiceType.TextToText, language='hi',
        )
        self.translate.assert_called_once_with(
            voice_provider=self.voice, message_body='Understood.',
            target_language='hi', source_language='en',
        )
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[1]['text']['msg'], '')

    def test_saved_query_is_translated(self):
        self.translate.return_value = {'status': 200, 'content': 'आपका विषय क्या है?'}
        _, saved = self.run_turn(query='What is your subject?')
        self.assertEqual(saved['message'], 'What is your subject?')
        self.assertEqual(saved['translated_message'], 'आपका विषय क्या है?')
        self.assertEqual(self.translate.call_args.kwargs['message_body'], 'What is your subject?')
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[1]['text']['msg'], '')

    def test_english_needs_no_translation(self):
        for query in ('', 'What is your subject?'):
            with self.subTest(query=query):
                _, saved = self.run_turn(language='en', query=query)
                self.assertIsNone(saved['translated_message'])
        self.translate.assert_not_called()
        self.voices.filter.assert_not_called()

    def test_normal_reply_still_translated_once_and_sent(self):
        self.handler._handle_regular_response(
            response='Hello.', chat_session=SimpleNamespace(current_step=1), company_bot=self.bot,
            session_id='s1', channel_name='c1', language='hi', profile_id=None,
            chunks=[], current_step=1,
        )
        self.assertEqual(self.save.call_args.kwargs['message'], 'Hello.')
        self.assertEqual(self.save.call_args.kwargs['translated_message'], 'समझ गया।')
        self.translate.assert_called_once()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[1]['text']['msg'], 'समझ गया।')

    def test_failed_translation_keeps_original_and_can_retry(self):
        failures = [RuntimeError('unavailable'), {'status': 500, 'content': 'error'},
                    {'status': 200, 'content': ''}, {'status': 200, 'content': '   '},
                    {'status': 200, 'content': None}, None]
        for failure in failures:
            with self.subTest(failure=failure):
                self.translate.side_effect = [failure, {'status': 200, 'content': 'समझ गया।'}]
                _, saved = self.run_turn()
                self.assertIsNone(saved['translated_message'])
                _, saved = self.run_turn()
                self.assertEqual(saved['translated_message'], 'समझ गया।')
                cache = getattr(common_handler, '_translate_handoff_placeholder', None)
                if cache:
                    cache.cache_clear()

    def test_missing_provider_keeps_original(self):
        self.voices.filter.return_value.first.return_value = None
        _, saved = self.run_turn()
        self.assertIsNone(saved['translated_message'])
        self.translate.assert_not_called()

    def test_provider_lookup_error_does_not_fail_turn(self):
        self.voices.filter.side_effect = RuntimeError('unavailable')
        _, saved = self.run_turn()
        self.assertIsNone(saved['translated_message'])
        self.send.assert_awaited_once()

    def test_placeholder_cache_separates_languages_and_providers(self):
        self.run_turn()
        self.run_turn()
        self.assertEqual(self.translate.call_count, 1)
        self.run_turn(language='mr')
        self.assertEqual(self.translate.call_count, 2)
        self.voices.filter.return_value.first.return_value = Voice(pk=35, provider='sarvam')
        self.run_turn()
        self.assertEqual(self.translate.call_count, 3)

    def test_queries_are_not_cached_as_fixed_placeholders(self):
        for query in ('First question?', 'Second question?'):
            self.run_turn(query=query)
        self.assertEqual(self.translate.call_count, 2)
        self.assertEqual(
            [call.kwargs['message_body'] for call in self.translate.call_args_list],
            ['First question?', 'Second question?'],
        )

    def test_streaming_completed_does_not_translate_or_save_again(self):
        self.run_turn(streaming_completed=True)
        self.translate.assert_not_called()
        self.save.assert_not_called()
        self.send.assert_not_awaited()
