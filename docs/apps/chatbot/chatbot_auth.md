# Chatbot Authentication

## Overview

Request-level authentication is enforced by a single Django middleware,
`VerifyAuthToken` — there is no DRF authentication class configured for this
purpose; `chatbot/auth.py` (a `JWTAuthentication` subclass named
`ProfileJWTAuthentication`) previously existed but was never wired into any
`DEFAULT_AUTHENTICATION_CLASSES` setting or view, and has been removed as
dead code.

WebSocket authentication is separate from both of the above and is handled
inline inside `AsyncSocketConsumer`'s `authenticate` message flow — see
[WebSocket Response Handling §2.3](chatbot_response_handlers.md).

## VerifyAuthToken Middleware

Located in `chatbot/middlewares/VerifyAuthToken.py`, registered in
`shikshalokam_mohini/settings.py`'s `MIDDLEWARE` list — runs on every request,
ahead of any view or DRF authentication.

- If an `Authorization` header is present, it must start with `Bearer `,
  otherwise the request is rejected with a 401 before it reaches any view.
- The bearer token is decoded with `jwt.decode(token, options={"verify_signature": False})`
  — signature verification is explicitly disabled here; this middleware only
  checks the token is well-formed JWT, not that it is valid or trusted. No
  downstream component re-verifies the signature or resolves a user from this
  token for HTTP requests.
- If the `Authorization` header is absent entirely, the request is passed
  through unauthenticated — this middleware only blocks a *malformed* token,
  never enforces that one is present.
