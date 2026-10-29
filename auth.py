"""OpenID Connect authentication for the web UI and JSON API.

Two ways to authenticate:

* **Browser pages** use the OIDC authorization-code flow (with PKCE) against
  Authelia and store the signed-in user in a Flask session cookie.
* **JSON API** calls (``/api/...``) are a resource-server interface and require
  ``Authorization: Bearer <jwt>``; they never fall back to the session.

Tokens and ID tokens are validated against the provider's JWKS (signature,
issuer, audience, expiry). Without a valid token or session the app returns
401 for API calls and redirects browser pages to ``/login``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

import jwt
import requests
from jwt import PyJWKClient

log = logging.getLogger("wa.auth")


class AuthError(Exception):
    """Base class for authentication failures."""


class MissingToken(AuthError):
    """No bearer token and no valid session were supplied."""


class InvalidToken(AuthError):
    """The supplied token is malformed, unsigned, expired or untrusted."""


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    audience: str
    jwks_uri: str = ""
    algorithms: tuple[str, ...] = ("RS256", "ES256", "PS256")
    leeway: int = 30
    disabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = "openid profile email"
    token_auth_method: str = "client_secret_basic"
    logout_url: str = ""

    @classmethod
    def from_env(cls) -> "OIDCConfig":
        issuer = os.environ.get("OIDC_ISSUER", "").strip().rstrip("/")
        client_id = os.environ.get("OIDC_CLIENT_ID", "").strip()
        audience = (
            os.environ.get("OIDC_AUDIENCE", "").strip() or client_id
        )
        jwks_uri = os.environ.get("OIDC_JWKS_URI", "").strip()
        algorithms = tuple(
            part.strip()
            for part in os.environ.get(
                "OIDC_ALGORITHMS", "RS256,ES256,PS256"
            ).split(",")
            if part.strip()
        )
        try:
            leeway = int(os.environ.get("OIDC_LEEWAY", "30"))
        except ValueError:
            leeway = 30
        disabled = os.environ.get("AUTH_DISABLED", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            issuer=issuer,
            audience=audience,
            jwks_uri=jwks_uri,
            algorithms=algorithms or ("RS256",),
            leeway=leeway,
            disabled=disabled,
            client_id=client_id,
            client_secret=os.environ.get("OIDC_CLIENT_SECRET", "").strip(),
            redirect_uri=os.environ.get("OIDC_REDIRECT_URI", "").strip(),
            scopes=os.environ.get(
                "OIDC_SCOPES", "openid profile email"
            ).strip(),
            token_auth_method=os.environ.get(
                "OIDC_TOKEN_AUTH_METHOD", "client_secret_basic"
            ).strip()
            or "client_secret_basic",
            logout_url=os.environ.get("OIDC_LOGOUT_URL", "").strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.audience)


class TokenValidator:
    """Validate signed JWTs against the provider's JWKS."""

    def __init__(self, config: OIDCConfig) -> None:
        self.config = config
        self._jwks_client: PyJWKClient | None = None
        self._lock = threading.Lock()

    def _client(self) -> PyJWKClient:
        with self._lock:
            if self._jwks_client is None:
                uri = self.config.jwks_uri or self._discover_jwks_uri()
                self._jwks_client = PyJWKClient(uri, cache_keys=True)
            return self._jwks_client

    def _discover_jwks_uri(self) -> str:
        url = f"{self.config.issuer}/.well-known/openid-configuration"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            document = response.json()
        except Exception as exc:
            raise InvalidToken(
                f"OIDC discovery failed for {url}: {exc}"
            ) from exc
        uri = document.get("jwks_uri")
        if not uri:
            raise InvalidToken("OIDC discovery document has no jwks_uri")
        return uri

    def validate(
        self, token: str, audience: str | None = None
    ) -> dict[str, Any]:
        expected_audience = audience or self.config.audience
        try:
            signing_key = self._client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.config.algorithms),
                audience=expected_audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except AuthError:
            raise
        except Exception as exc:
            raise InvalidToken(str(exc)) from exc
        return claims


def new_pkce_pair() -> tuple[str, str]:
    """Return a PKCE (code_verifier, code_challenge_S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = (
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    )
    return verifier, challenge


class OIDCLogin:
    """Authorization-code flow against the OIDC provider."""

    def __init__(
        self,
        config: OIDCConfig,
        validator: TokenValidator | None = None,
    ) -> None:
        self.config = config
        self.validator = validator or TokenValidator(config)
        self._discovery: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def discovery(self) -> dict[str, Any]:
        with self._lock:
            if self._discovery is None:
                url = (
                    f"{self.config.issuer}"
                    "/.well-known/openid-configuration"
                )
                try:
                    response = requests.get(url, timeout=10)
                    response.raise_for_status()
                    self._discovery = response.json()
                except Exception as exc:
                    raise InvalidToken(
                        f"OIDC discovery failed for {url}: {exc}"
                    ) from exc
            return self._discovery

    def _endpoint(self, name: str) -> str:
        value = self.discovery().get(name)
        if not value:
            raise AuthError(f"OIDC provider has no {name}")
        return value

    def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str:
        endpoint = self._endpoint("authorization_endpoint")
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": redirect_uri,
            "scope": self.config.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{urlencode(params)}"

    def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> dict[str, Any]:
        endpoint = self._endpoint("token_endpoint")
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": code_verifier,
        }
        auth = None
        method = self.config.token_auth_method
        if self.config.client_secret:
            if method == "client_secret_post":
                payload["client_secret"] = self.config.client_secret
            elif method not in {"none", "public"}:
                auth = (self.config.client_id, self.config.client_secret)
        try:
            response = requests.post(
                endpoint, data=payload, auth=auth, timeout=15
            )
        except Exception as exc:
            raise InvalidToken(f"token request failed: {exc}") from exc
        if response.status_code >= 400:
            raise InvalidToken(
                "token endpoint returned "
                f"{response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidToken(
                "token endpoint returned invalid JSON"
            ) from exc

    def userinfo(self, access_token: str) -> dict[str, Any]:
        """Fetch profile claims from the provider's userinfo endpoint."""
        endpoint = self.discovery().get("userinfo_endpoint")
        if not endpoint or not access_token:
            return {}
        try:
            response = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
        except Exception:
            log.warning("userinfo request failed", exc_info=True)
            return {}
        if response.status_code != 200:
            log.warning(
                "userinfo endpoint returned %s", response.status_code
            )
            return {}
        try:
            data = response.json()
        except ValueError:
            log.warning("userinfo endpoint returned invalid JSON")
            return {}
        return data if isinstance(data, dict) else {}

    def validate_id_token(
        self, id_token: str, nonce: str = ""
    ) -> dict[str, Any]:
        claims = self.validator.validate(
            id_token, audience=self.config.client_id
        )
        if nonce and claims.get("nonce") != nonce:
            raise InvalidToken("nonce mismatch")
        return claims

    def logout_url(
        self, id_token: str = "", post_logout_redirect_uri: str = ""
    ) -> str:
        try:
            endpoint = self.discovery().get("end_session_endpoint")
        except AuthError:
            endpoint = None
        if not endpoint:
            return ""
        params: dict[str, str] = {}
        if id_token:
            params["id_token_hint"] = id_token
        if post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = post_logout_redirect_uri
        return endpoint + (
            ("?" + urlencode(params)) if params else ""
        )


class AuthGate:
    """Authorize requests via bearer token or browser session."""

    def __init__(
        self,
        config: OIDCConfig | None = None,
        validator: TokenValidator | None = None,
        login: OIDCLogin | None = None,
    ) -> None:
        self.config = config or OIDCConfig.from_env()
        self.validator = validator or TokenValidator(self.config)
        self.login = login or OIDCLogin(self.config, self.validator)

    @property
    def enabled(self) -> bool:
        return not self.config.disabled

    @property
    def can_login(self) -> bool:
        return self.enabled and bool(
            self.config.issuer and self.config.client_id
        )

    @staticmethod
    def extract_token(headers: Mapping[str, str]) -> str:
        value = headers.get("Authorization", "")
        scheme, _, token = value.partition(" ")
        if scheme.strip().lower() != "bearer":
            return ""
        return token.strip()

    def check(self, headers: Mapping[str, str]) -> dict[str, Any]:
        """Validate a bearer token only (used by the JSON API)."""
        if not self.enabled:
            return {}
        token = self.extract_token(headers)
        if not token:
            raise MissingToken("missing bearer token")
        return self.validator.validate(token)

    def authorize(
        self,
        headers: Mapping[str, str],
        session_user: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Accept a bearer token or a still-valid browser session."""
        if not self.enabled:
            return {}
        token = self.extract_token(headers)
        if token:
            return self.validator.validate(token)
        if session_user:
            expires = session_user.get("exp")
            if (
                isinstance(expires, (int, float))
                and expires > time.time() - self.config.leeway
            ):
                return dict(session_user)
        raise MissingToken("not logged in")


def build_auth_from_env() -> AuthGate | None:
    """Return a configured gate, or ``None`` when auth is turned off."""
    config = OIDCConfig.from_env()
    if config.disabled:
        log.warning("AUTH_DISABLED is set; web/API authentication is turned off")
        return None
    if not config.configured:
        log.warning(
            "OIDC is not configured (set OIDC_ISSUER and OIDC_CLIENT_ID); "
            "web/API authentication is turned off"
        )
        return None
    log.info("OIDC authentication enabled (issuer=%s)", config.issuer)
    return AuthGate(config)
