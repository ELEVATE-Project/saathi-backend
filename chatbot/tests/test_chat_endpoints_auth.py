"""T32 (ISS-45): /api/companychat/ and /api/chatsession/ need a verified login and show only the
caller's own rows. Writes are not allowed on these paths."""
import os
import time
from unittest import mock

import jwt
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from chatbot.models import ChatSession, Company, Profile
from chatbot.models.company_models import CompanyChat

KEY = 'test-signing-key-for-t32-only-0123456789abcdef'
OTHER_KEY = 'a-different-signing-key-0123456789abcdefghij'


def make_token(user_id, key=KEY, expires_in=3600, claims=None):
    payload = {'data': {'id': user_id, 'name': 'T'}, 'exp': int(time.time()) + expires_in}
    if claims is not None:
        payload = claims
    return jwt.encode(payload, key, algorithm='HS256')


def results(response):
    data = response.json()
    return data['results'] if isinstance(data, dict) and 'results' in data else data


class ChatEndpointAuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company = Company.objects.create(name='C', slug='c-t32', status='ACTIVE')
        cls.profile_a = Profile.objects.create(userid='user-a', company=company, first_name='A')
        cls.profile_b = Profile.objects.create(userid='user-b', company=company, first_name='B')
        now = timezone.now()

        # A1: made by create_chatsession (profile only, no user_id).
        cls.a1 = ChatSession.objects.create(session='sess-a1', profile=cls.profile_a, project_id='proj-a')
        # A2: made by the websocket path (user_id set; profile may be set too).
        cls.a2 = ChatSession.objects.create(session='sess-a2', user_id='user-a')
        cls.b1 = ChatSession.objects.create(session='sess-b1', profile=cls.profile_b,
                                            user_id='user-b', project_id='proj-b')
        # A session nobody owns must never show up for anyone.
        cls.orphan = ChatSession.objects.create(session='sess-orphan')

        def chat(session, text, sender, receiver):
            return CompanyChat.objects.create(
                message=text, session=session, sender=sender, receiver=receiver, created_at=now)

        cls.chat_a1 = chat('sess-a1', 'a1-question', cls.profile_a, None)
        cls.chat_a2 = chat('sess-a2', 'a2-question', None, None)
        cls.chat_b1 = chat('sess-b1', 'b1-question', cls.profile_b, None)
        cls.chat_b1_reply = chat('sess-b1', 'b1-answer', None, cls.profile_b)
        cls.chat_orphan = chat('sess-orphan', 'orphan-msg', None, None)
        cls.chat_nosession = chat('no-such-session', 'floating-msg', cls.profile_a, None)

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {'JWT_PUBLIC_KEY': KEY})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = APIClient()

    def get(self, path, token=None, **params):
        headers = {'HTTP_X_AUTH_TOKEN': token} if token else {}
        return self.client.get(path, params, **headers)

    def as_a(self, path, **params):
        return self.get(path, make_token('user-a'), **params)

    # ---- no login, bad login -------------------------------------------------------------

    def all_paths(self):
        return [
            '/api/companychat/',
            f'/api/companychat/{self.chat_a1.pk}/',
            '/api/chatsession/',
            f'/api/chatsession/{self.a1.pk}/',
            '/api/chatsession/sess-a1/',
        ]

    def test_no_token_is_401_on_every_path(self):
        for path in self.all_paths():
            with self.subTest(path=path):
                self.assertEqual(self.get(path).status_code, 401)

    def test_bad_tokens_are_401_on_every_path(self):
        bad = {
            'wrong signature': make_token('user-a', key=OTHER_KEY),
            'expired': make_token('user-a', expires_in=-60),
            'garbage': 'not-a-jwt',
            'no user id': make_token(None, claims={'data': {}, 'exp': int(time.time()) + 3600}),
            'empty user id': make_token(None, claims={'data': {'id': ''}, 'exp': int(time.time()) + 3600}),
            'no data claim': make_token(None, claims={'exp': int(time.time()) + 3600}),
            'alg none': jwt.encode({'data': {'id': 'user-a'}}, None, algorithm='none'),
        }
        for label, token in bad.items():
            for path in self.all_paths():
                with self.subTest(token=label, path=path):
                    self.assertEqual(self.get(path, token).status_code, 401)

    def test_server_without_a_key_rejects_everything(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('JWT_PUBLIC_KEY')
            for path in self.all_paths():
                with self.subTest(path=path):
                    self.assertEqual(self.get(path, make_token('user-a')).status_code, 401)

    def test_unusable_server_key_gives_401_not_500(self):
        pem_text = '-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----'
        with mock.patch.dict(os.environ, {'JWT_PUBLIC_KEY': pem_text}):
            for path in self.all_paths():
                with self.subTest(path=path):
                    self.assertEqual(self.get(path, make_token('user-a')).status_code, 401)

    def test_authorization_header_alone_does_not_log_in(self):
        for path in self.all_paths():
            with self.subTest(path=path):
                response = self.client.get(path, HTTP_AUTHORIZATION='Bearer ' + make_token('user-a'))
                self.assertEqual(response.status_code, 401)

    # ---- owner-only reads ----------------------------------------------------------------

    def test_session_list_has_only_the_callers_sessions(self):
        response = self.as_a('/api/chatsession/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({r['session'] for r in results(response)}, {'sess-a1', 'sess-a2'})

    def test_message_list_has_only_the_callers_messages(self):
        response = self.as_a('/api/companychat/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({r['message'] for r in results(response)}, {'a1-question', 'a2-question'})

    def test_numeric_user_id_in_token_matches_string_userid(self):
        company = Company.objects.get(slug='c-t32')
        Profile.objects.create(userid='42', company=company)
        ChatSession.objects.create(session='sess-42', profile=Profile.objects.get(userid='42'))
        response = self.get('/api/chatsession/', make_token(42))
        self.assertEqual({r['session'] for r in results(response)}, {'sess-42'})

    def test_own_detail_rows_are_readable(self):
        self.assertEqual(self.as_a(f'/api/companychat/{self.chat_a1.pk}/').status_code, 200)
        self.assertEqual(self.as_a(f'/api/chatsession/{self.a1.pk}/').status_code, 200)
        response = self.as_a('/api/chatsession/sess-a1/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['session'], 'sess-a1')

    def test_other_users_detail_rows_are_404_not_403(self):
        for path in (
            f'/api/companychat/{self.chat_b1.pk}/',
            f'/api/chatsession/{self.b1.pk}/',
            '/api/chatsession/sess-b1/',
            f'/api/companychat/{self.chat_orphan.pk}/',
            '/api/chatsession/sess-orphan/',
        ):
            with self.subTest(path=path):
                self.assertEqual(self.as_a(path).status_code, 404)

    def test_filters_cannot_widen_the_session_list(self):
        for params in (
            {'user_id': 'user-b'},
            {'profile': self.profile_b.pk},
            {'session': 'sess-b1'},
            {'project_id': 'proj-b'},
            {'flow': 'anything'},
            {'session_type': 'x'},
        ):
            with self.subTest(params=params):
                response = self.as_a('/api/chatsession/', **params)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('sess-b1', {r['session'] for r in results(response)})
                self.assertNotIn('sess-orphan', {r['session'] for r in results(response)})
        # The ones that name B must return nothing at all.
        for params in ({'user_id': 'user-b'}, {'profile': self.profile_b.pk},
                       {'session': 'sess-b1'}, {'project_id': 'proj-b'}):
            with self.subTest(empty=params):
                self.assertEqual(results(self.as_a('/api/chatsession/', **params)), [])

    def test_the_frontends_own_filters_still_work_for_the_owner(self):
        response = self.as_a('/api/chatsession/', project_id='proj-a', ordering='-updated_at', limit=100)
        self.assertEqual({r['session'] for r in results(response)}, {'sess-a1'})
        response = self.as_a('/api/chatsession/', session='sess-a2')
        self.assertEqual({r['session'] for r in results(response)}, {'sess-a2'})

    def test_messages_of_another_session_are_never_returned(self):
        for params in (
            {'session': 'sess-b1'},
            {'session': 'sess-orphan'},
            {'session': 'no-such-session'},
            {'sender': self.profile_b.pk},
            {'receiver': self.profile_b.pk},
            {'status': ''},
        ):
            with self.subTest(params=params):
                response = self.as_a('/api/companychat/', **params)
                self.assertIn(response.status_code, (200, 404))
                if response.status_code == 200:
                    messages = {r['message'] for r in results(response)}
                    self.assertFalse(messages & {'b1-question', 'b1-answer', 'orphan-msg', 'floating-msg'})

    def test_messages_of_own_session_load(self):
        response = self.as_a('/api/companychat/', session='sess-a1')
        self.assertEqual([r['message'] for r in results(response)], ['a1-question'])
        for key in ('id', 'message', 'session', 'sender', 'thumbs_up', 'thumbs_down'):
            self.assertIn(key, results(response)[0])

    # ---- no writes -----------------------------------------------------------------------

    def test_write_methods_are_rejected_for_everyone(self):
        before = (
            list(CompanyChat.objects.order_by('pk').values_list('pk', 'message')),
            list(ChatSession.objects.order_by('pk').values_list('pk', 'session', 'title', 'user_id')),
        )
        body = {'message': 'changed', 'title': 'changed', 'session': 'sess-new', 'user_id': 'user-a'}
        for path in self.all_paths():
            for method in ('post', 'put', 'patch', 'delete'):
                for label, headers in (
                    ('anonymous', {}),
                    ('owner', {'HTTP_X_AUTH_TOKEN': make_token('user-a')}),
                    ('other user', {'HTTP_X_AUTH_TOKEN': make_token('user-b')}),
                ):
                    with self.subTest(path=path, method=method, caller=label):
                        response = getattr(self.client, method)(path, body, format='json', **headers)
                        self.assertIn(response.status_code, (401, 403, 405))
        after = (
            list(CompanyChat.objects.order_by('pk').values_list('pk', 'message')),
            list(ChatSession.objects.order_by('pk').values_list('pk', 'session', 'title', 'user_id')),
        )
        self.assertEqual(before, after)

    def test_a_logged_in_caller_gets_405_not_a_write(self):
        for path in self.all_paths():
            for method in ('post', 'put', 'patch', 'delete'):
                with self.subTest(path=path, method=method):
                    response = getattr(self.client, method)(
                        path, {}, format='json', HTTP_X_AUTH_TOKEN=make_token('user-a'))
                    self.assertEqual(response.status_code, 405)


class CreateChatsessionStillVerifiesTests(TestCase):
    """create_chatsession shares the decode helper with the new endpoints (T32)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {'JWT_PUBLIC_KEY': KEY})
        patcher.start()
        self.addCleanup(patcher.stop)
        Company.objects.create(name='S', slug='shikshalokamstaging', status='ACTIVE')
        self.body = {'session': 'sess-new', 'email': 'x@example.com', 'preferred_language': {'value': 'en'}}

    def post(self, token=None):
        headers = {'HTTP_X_AUTH_TOKEN': token} if token else {}
        return APIClient().post('/api/create-chatsession/', self.body, format='json', **headers)

    def test_missing_wrong_and_expired_tokens_are_401(self):
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.post(make_token('u', key=OTHER_KEY)).status_code, 401)
        self.assertEqual(self.post(make_token('u', expires_in=-60)).status_code, 401)
        self.assertFalse(ChatSession.objects.filter(session='sess-new').exists())

    def test_valid_token_creates_the_session_for_that_user(self):
        response = self.post(make_token('user-new'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ChatSession.objects.get(session='sess-new').profile.userid, 'user-new')
        # And that new session is visible to its owner through the read endpoint.
        listing = APIClient().get('/api/chatsession/', HTTP_X_AUTH_TOKEN=make_token('user-new'))
        self.assertEqual({r['session'] for r in results(listing)}, {'sess-new'})
