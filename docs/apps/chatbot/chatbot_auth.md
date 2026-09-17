# Chatbot Authentication

## Overview

The authentication layer in the Chatbot app is responsible for validating user identity, securing access, and managing JWT tokens.

## ProfileJWTAuthentication Class

Located in `chatbot/auth.py`, this class extends JWTAuthentication from rest_framework_simplejwt to:

- Authenticate users based on JWT tokens.
- Retrieve user profile information from the `Profile` model.
- Ensure blacklisted tokens cannot be used.

### Key Methods

- `authenticate(request)`: Verifies presence of Authorization header, validates token, checks blacklist.
- `get_user(validated_token)`: Extracts the user from the token, raises errors if token invalid or user not found.

### Token Blacklisting

- Utilizes `BlacklistedToken` model.
- Checks if token is blacklisted and denies authentication if so.

## VerifyAuthToken Middleware

Located in `chatbot/middlewares/VerifyAuthToken.py`, registered in
`shikshalokam_mohini/settings.py`'s `MIDDLEWARE` list — runs on every request,
ahead of DRF's own authentication.

- If an `Authorization` header is present, it must start with `Bearer `,
  otherwise the request is rejected with a 401 before it reaches any view.
- The bearer token is decoded with `jwt.decode(token, options={"verify_signature": False})`
  — signature verification is explicitly disabled here; this middleware only
  checks the token is well-formed JWT, not that it is valid or trusted. Actual
  signature/expiry verification and user resolution happen downstream in
  `ProfileJWTAuthentication`.
- If the `Authorization` header is absent entirely, the request is passed
  through unauthenticated — this middleware only blocks a *malformed* token,
  never enforces that one is present.

## Interaction

- Integrated directly with Django Rest Framework authentication flow.
- Used across chatbot endpoints for secure access.
