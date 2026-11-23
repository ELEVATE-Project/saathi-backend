"""Verified login for the chat read endpoints (T32, ISS-45).

One decode is shared by `create_chatsession` and the DRF views, so there is only one place that
checks the token signature. The caller is returned as an identity object with a `kind`, so a
different kind of caller (for example a guest) can be added later without changing the views.
"""
import os

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

TOKEN_HEADER = 'X-auth-token'


def decode_access_token(token):
    """Return the token's claims. Raise ExpiredSignatureError or InvalidTokenError.

    Fails closed: with no JWT_PUBLIC_KEY on the server, every token is rejected.
    """
    key = os.getenv('JWT_PUBLIC_KEY')
    if not key:
        raise InvalidTokenError('JWT_PUBLIC_KEY is not set')
    return jwt.decode(token, key, algorithms=['HS256'])


def user_id_from_claims(claims):
    """The user id in the token as a string, or None when it is missing or empty."""
    data = claims.get('data') if isinstance(claims, dict) else None
    user_id = data.get('id') if isinstance(data, dict) else None
    if user_id is None or isinstance(user_id, (dict, list, bool)):
        return None
    user_id = str(user_id).strip()
    return user_id or None


class CallerIdentity:
    """Who is calling. Only 'teacher' exists today."""
    is_authenticated = True
    is_anonymous = False

    def __init__(self, user_id, kind='teacher'):
        self.user_id = user_id
        self.kind = kind


class VerifiedTokenAuthentication(BaseAuthentication):
    """Reads the X-auth-token header and verifies it. No header means "not logged in" (the
    permission class then answers 401). A bad, expired or user-less token is a 401 at once."""

    def authenticate(self, request):
        token = request.headers.get(TOKEN_HEADER)
        if not token:
            return None
        try:
            claims = decode_access_token(token)
        except ExpiredSignatureError:
            raise AuthenticationFailed('Access token expired')
        except InvalidTokenError:
            raise AuthenticationFailed('Invalid access token')
        user_id = user_id_from_claims(claims)
        if not user_id:
            raise AuthenticationFailed('Invalid access token')
        return CallerIdentity(user_id), token

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when the caller is not logged in.
        return TOKEN_HEADER


class IsVerifiedCaller(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.user, CallerIdentity)
