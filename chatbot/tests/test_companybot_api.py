from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIClient

from chatbot.serializer.company_serializer import (
    BotVernacularSerializer, CompanyBotSerializer,
)


class PublicBotMetadataTests(SimpleTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(
            id=123, name='Test bot', company=SimpleNamespace(name='Test', slug='test'),
            route='/test', bot_type='SIMPLE', introductory_message='Welcome',
            companystatemachine_set=Mock(count=Mock(return_value=2)),
            context='SECRET_PROMPT', dynamic_context='SECRET_SQL',
            provider_keys='SECRET_PROVIDER_KEY', llm_key='SECRET_LLM_KEY',
        )

    def test_public_bot_contains_only_chat_metadata(self):
        data = CompanyBotSerializer(self.bot).data
        self.assertEqual(set(data), {
            'id', 'name', 'company', 'route', 'bot_type',
            'introductory_message', 'statemachine_length',
        })
        self.assertEqual(data['statemachine_length'], 2)
        self.assertEqual(data['route'], '/test')
        self.assertNotIn('SECRET_', str(data))

    def test_nested_vernacular_bot_uses_same_public_fields(self):
        nested_bot = BotVernacularSerializer().fields['company_bot']
        self.assertEqual(nested_bot.to_representation(self.bot), CompanyBotSerializer(self.bot).data)
        self.assertNotIn('SECRET_', str(nested_bot.to_representation(self.bot)))

    def test_public_bot_fields_cannot_be_written_through_serializer(self):
        serializer = CompanyBotSerializer(self.bot, data={
            'name': 'Changed', 'context': 'Changed', 'dynamic_context': 'Changed',
        }, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {})

    def test_public_endpoints_reject_every_mutation_without_database_access(self):
        client = APIClient()
        for path in (
            '/api/companybot/', '/api/companybot/123/',
            '/api/bot_vernacular/', '/api/bot_vernacular/123/',
        ):
            for method in ('post', 'put', 'patch', 'delete'):
                with self.subTest(path=path, method=method):
                    response = getattr(client, method)(path, {'context': 'Changed'}, format='json')
                    self.assertEqual(response.status_code, 405)

    def test_detail_response_preserves_frontend_metadata_without_secrets(self):
        from chatbot.views.drf_views import CompanyBotRetrieveUpdateDestroyView

        with patch.object(CompanyBotRetrieveUpdateDestroyView, 'get_object', return_value=self.bot):
            response = APIClient().get('/api/companybot/123/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['bot_type'], 'SIMPLE')
        self.assertNotIn('context', response.data)

    def test_options_does_not_advertise_config_writes_or_fields(self):
        response = APIClient().options('/api/companybot/123/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PATCH', response['Allow'])
        self.assertNotIn('PUT', response['Allow'])
        self.assertNotIn('SECRET_', str(response.data))

    def test_views_expose_no_write_method(self):
        from chatbot.views import drf_views

        for view in (
            drf_views.CompanyBotListCreateView,
            drf_views.CompanyBotRetrieveUpdateDestroyView,
            drf_views.BotVernacularListCreateView,
            drf_views.BotVernacularRetrieveUpdateDestroyView,
        ):
            with self.subTest(view=view.__name__):
                allowed = {m.upper() for m in view.http_method_names if hasattr(view, m)}
                self.assertTrue(allowed <= {'GET', 'HEAD', 'OPTIONS'}, allowed)

    def test_llm_model_filter_is_not_offered(self):
        from chatbot.views.drf_views import CompanyBotListCreateView

        self.assertNotIn('llm_model', CompanyBotListCreateView.filterset_fields)
