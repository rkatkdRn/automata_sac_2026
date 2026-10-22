"""Verify Google ID tokens using Google's supported signature verifier.

This module never accepts a decoded browser profile as proof of identity. The
caller must additionally enforce the school's claims and browser-bound nonce.
"""
from __future__ import annotations


class GoogleVerificationError(Exception):
    def __init__(self, unavailable=False):
        self.unavailable = unavailable
        super().__init__('Google identity verification unavailable' if unavailable else 'Invalid Google identity token')


def verify_credential(credential: str, audience: str) -> dict:
    """Verify signature, audience, issuer and token lifetime; bound network waits."""
    try:
        from google.auth import exceptions
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token
    except ImportError:
        raise GoogleVerificationError(unavailable=True) from None

    class BoundedRequest(Request):
        def __call__(self, url, method='GET', body=None, headers=None, timeout=None, **kwargs):
            # Google's certificate fetch otherwise defaults to a 120-second wait.
            return super().__call__(url, method=method, body=body, headers=headers,
                                    timeout=(3, 7), **kwargs)

    request = BoundedRequest()
    try:
        return dict(id_token.verify_oauth2_token(credential, request, audience=audience))
    except exceptions.TransportError:
        raise GoogleVerificationError(unavailable=True) from None
    except (ValueError, TypeError, KeyError, exceptions.GoogleAuthError):
        raise GoogleVerificationError() from None
    finally:
        request.session.close()
