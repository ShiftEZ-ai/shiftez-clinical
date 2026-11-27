"""Authorization Code + PKCE + nonce. Only explicitly bound identities get access.

No remote role claims or email/name matching grant application permissions.
Institutional IdP compatibility still requires live qualification.
"""
import base64
import hashlib
from ipaddress import ip_address
import math
import re
import secrets
import socket
from datetime import timedelta
from threading import RLock
import time
from urllib.parse import quote_plus, urlencode, urlparse
import httpx
import jwt
from sqlalchemy import delete, select
from . import models as m
from .auth import allowed_departments, digest, issue_session, revoke_session, session_event
from .errors import require, Problem
from packages.contracts.schemas import valid_oidc_host
from packages.domain.time import aware


JWKS_TTL_SECONDS = 300
JWKS_REFRESH_SECONDS = 30
JWKS_CACHE_LIMIT = 16
_JWKS_CACHE = {}
_JWKS_LOCK = RLock()


def _key_supports(key, algorithm):
    return (key.get("use", "sig") == "sig" and key.get("alg", algorithm) == algorithm
            and key.get("kty") == ("RSA" if algorithm == "RS256" else "EC")
            and (algorithm != "ES256" or key.get("crv") == "P-256")
            and ("kid" not in key or isinstance(key["kid"], str) and 0 < len(key["kid"]) <= 255)
            and ("key_ops" not in key or isinstance(key["key_ops"], list)
                 and all(isinstance(operation, str) for operation in key["key_ops"]) and "verify" in key["key_ops"]))


def _parse_endpoint(settings, url):
    require(isinstance(url, str), "OIDC_ENDPOINT_REJECTED", "Invalid identity provider endpoint", 503)
    try:
        parsed = urlparse(url)
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        raise Problem(503, "OIDC_ENDPOINT_REJECTED", "Invalid identity provider endpoint") from None
    allowed = {s.strip().lower() for s in settings.oidc_allowed_hosts.split(",") if s.strip()}
    require(valid_port and not any(c.isspace() or ord(c) < 32 for c in url) and "\\" not in url
            and parsed.scheme == "https" and parsed.hostname and parsed.hostname.lower() in allowed
            and valid_oidc_host(parsed.hostname.lower())
            and parsed.username is None and parsed.password is None and not parsed.fragment,
            "OIDC_ENDPOINT_REJECTED", "Identity provider endpoint is outside the configured HTTPS host allowlist", 503)
    return parsed


def _allowed_endpoint(settings, url):
    parsed = _parse_endpoint(settings, url)
    try:
        addresses = list(dict.fromkeys(result[4][0] for result in socket.getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)))
        public = bool(addresses) and all("%" not in value and ip_address(value).is_global
                                        and not ip_address(value).is_multicast for value in addresses)
    except (OSError, ValueError):
        public = False
    require(public, "OIDC_ENDPOINT_REJECTED", "Identity provider endpoints must resolve only to public network addresses", 503)
    return parsed, addresses


def allowed_url(settings, url):
    _allowed_endpoint(settings, url)
    return url


def _request_json(settings, method, url, **kwargs):
    parsed, addresses = _allowed_endpoint(settings, url)
    target = httpx.URL(url)
    headers = dict(kwargs.pop("headers", {})) | {"Host": target.netloc.decode("ascii")}
    # Pin the validated address while retaining the provider's HTTP host and TLS certificate name.
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        for index, address in enumerate(addresses):
            try:
                response = getattr(client, method)(target.copy_with(host=address), headers=headers,
                                                   extensions={"sni_hostname": parsed.hostname}, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise
                continue
            response.raise_for_status()
            return response.json()


# An in-app address only: "#page/part?query" with URL-safe characters. It is put after "/" on this origin, so it can
# never send the browser elsewhere (no scheme, host, path or backslash can get through).
RETURN_ROUTE = re.compile(r"#[A-Za-z0-9_\-./?=&%:~,+]{0,500}")


def return_route(value):
    """The address to return to after sign-in, or "" when it is not a plain in-app address (no open redirect)."""
    return value if isinstance(value, str) and RETURN_ROUTE.fullmatch(value) and "//" not in value else ""


def document(settings):
    require(bool(settings.oidc_issuer), "OIDC_UNCONFIGURED", "Institutional sign-in is not configured", 503)
    config = _request_json(settings, "get", getattr(settings, "oidc_discovery_url", "")
                           or settings.oidc_issuer + "/.well-known/openid-configuration")
    require(isinstance(config, dict), "OIDC_DISCOVERY_INVALID", "Invalid identity provider discovery document", 503)
    require(config.get("issuer") == settings.oidc_issuer, "OIDC_ISSUER_MISMATCH", "Unexpected provider issuer", 503)
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        allowed_url(settings, config.get(name, ""))
    methods = config.get("code_challenge_methods_supported", [])
    require(isinstance(methods, list) and "S256" in methods,
            "OIDC_PKCE_REQUIRED", "Provider must support S256 PKCE", 503)
    for name, required in (("response_types_supported", "code"), ("grant_types_supported", "authorization_code")):
        if name in config:
            require(isinstance(config[name], list) and all(isinstance(value, str) for value in config[name])
                    and required in config[name],
                    "OIDC_CODE_REQUIRED", "Provider must support authorization code sign-in", 503)
    algorithms = config.get("id_token_signing_alg_values_supported", ["RS256"])
    require(isinstance(algorithms, list) and all(isinstance(algorithm, str) for algorithm in algorithms)
            and any(algorithm in {"RS256", "ES256"} for algorithm in algorithms),
            "OIDC_ALGORITHM", "Provider must support asymmetric identity signatures", 503)
    return config


def signing_keys(settings, uri, *, refresh=False):
    """Cache only validated provider keys; an unfamiliar kid gets one bounded refresh per interval."""
    uri = allowed_url(settings, uri)
    cache_key = (settings.oidc_issuer, uri, settings.oidc_allowed_hosts,
                 getattr(settings, "auth_configuration_revision", None))
    with _JWKS_LOCK:
        moment = time.monotonic()
        cached = _JWKS_CACHE.get(cache_key)
        if cached and moment < cached[0] and (not refresh or moment < cached[2]):
            return cached[1]
        value = _request_json(settings, "get", uri)
        require(isinstance(value, dict) and isinstance(value.get("keys"), list)
                and len(value["keys"]) <= 64 and all(isinstance(key, dict) for key in value["keys"]),
                "OIDC_SIGNING_KEY", "Invalid identity provider signing keys", 401)
        keys = []
        for key in value["keys"]:
            for algorithm in ("RS256", "ES256"):
                if _key_supports(key, algorithm):
                    try:
                        parsed = jwt.PyJWK.from_dict(key, algorithm=algorithm).key
                    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
                        continue
                    if algorithm == "RS256" and parsed.key_size < 2048:
                        continue
                    keys.append(key)
                    break
        value = {"keys": keys}
        if cache_key not in _JWKS_CACHE and len(_JWKS_CACHE) >= JWKS_CACHE_LIMIT:
            _JWKS_CACHE.pop(next(iter(_JWKS_CACHE)))
        _JWKS_CACHE[cache_key] = (moment + JWKS_TTL_SECONDS, value,
                                 moment + JWKS_REFRESH_SECONDS if refresh else cached[2] if cached else 0)
        return value


def callback_uri(settings):
    expected = settings.public_origin.rstrip("/") + "/auth/callback"
    require(settings.oidc_redirect_uri == expected, "OIDC_CALLBACK", "Identity callback must use this deployment's public origin", 503)
    return expected


def logout_url(settings, auth_method, state):
    """Discovery failure must never prevent local logout; the browser then requires fresh authentication."""
    if auth_method != "oidc":
        return None
    try:
        endpoint = document(settings).get("end_session_endpoint")
        if not endpoint:
            return None
        allowed_url(settings, endpoint)
        params = {"client_id": settings.oidc_client_id,
                  "post_logout_redirect_uri": settings.public_origin + "/", "state": state}
        return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)
    except (Problem, httpx.HTTPError, ValueError, TypeError):
        return None


def start_flow(db, settings, config, *, force_login=False, browser=None, remember_me=False):
    # Provider discovery happens outside the institution-wide write transaction.
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    browser = browser or secrets.token_urlsafe(32)
    # Anyone can start sign-in, so abandoned flows are cleared here rather than piling up.
    db.execute(delete(m.OIDCFlow).where(m.OIDCFlow.expires_at < m.now()))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    flow = m.OIDCFlow(state_hash=digest(state), browser_hash=digest(browser), nonce=nonce,
                      verifier=verifier, force_login=force_login, expires_at=m.now() + timedelta(minutes=5))
    flow.remember_me = remember_me
    flow.config_revision = getattr(settings, "auth_configuration_revision", None)
    db.add(flow)
    params = {"response_type": "code", "client_id": settings.oidc_client_id,
              "redirect_uri": callback_uri(settings), "scope": " ".join(getattr(settings, "oidc_scopes", ["openid", "profile"])),
              "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    if force_login:
        # prompt=login alone: Authelia 4.39 never satisfies max_age=0 (the re-auth is already >0 s old when checked,
        # so its consent step hangs); freshness is enforced here instead via auth_time (consume_flow, ±30 s).
        params.update(prompt="login")
    endpoint = config["authorization_endpoint"]
    _parse_endpoint(settings, endpoint)
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params), browser


def consume_flow(db, state, browser):
    flow = db.scalar(select(m.OIDCFlow).where(m.OIDCFlow.state_hash == digest(state)).with_for_update())
    require(flow is not None and not flow.used and aware(flow.expires_at) > m.now()
            and browser and secrets.compare_digest(flow.browser_hash, digest(browser)),
            "OIDC_STATE", "Sign-in state expired or is not bound to this browser; restart sign-in", 401)
    flow.used = True
    # Return only the ephemeral exchange material; committed as used before remote I/O.
    return {"id": flow.id, "nonce": flow.nonce, "verifier": flow.verifier,
            "remember_me": getattr(flow, "remember_me", False), "config_revision": getattr(flow, "config_revision", None),
            "reauthenticate_after": aware(flow.created_at).timestamp() if flow.force_login else None}


def cancel_flow(db, browser, correlation_id=None, *, continuation=None):
    """Cancel the browser's pending callback, including a session whose response is still in flight."""
    if not browser:
        return
    query = select(m.OIDCFlow).where(m.OIDCFlow.browser_hash == digest(browser)).with_for_update()
    if continuation is not None:
        query = query.where(m.OIDCFlow.state_hash == digest(continuation))
    for flow in db.scalars(query):
        flow.expires_at = m.now()
        session = db.get(m.Session, flow.completed_session_id) if flow.completed_session_id else None
        if session is not None and not session.revoked:
            revoke_session(session)
            session_event(db, session, db.get(m.Identity, session.identity_id).person_id, "auth.sign_out",
                          correlation_id=correlation_id)


def exchange(settings, flow, code):
    config = document(settings)
    methods = config.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
    require(isinstance(methods, list) and all(isinstance(method, str) for method in methods),
            "OIDC_CLIENT_AUTH_METHOD", "Invalid provider client authentication methods", 503)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": callback_uri(settings),
            "client_id": settings.oidc_client_id, "code_verifier": flow["verifier"]}
    auth = None
    if settings.oidc_client_secret:
        if "client_secret_basic" in methods:
            auth = (quote_plus(settings.oidc_client_id), quote_plus(settings.oidc_client_secret))
        else:
            require("client_secret_post" in methods, "OIDC_CLIENT_AUTH_METHOD", "Provider does not support this client's authentication method", 503)
            data["client_secret"] = settings.oidc_client_secret
    else:
        require("none" in methods, "OIDC_CLIENT_AUTH_METHOD", "Provider does not support public PKCE clients", 503)
    tokens = _request_json(settings, "post", config["token_endpoint"], data=data, auth=auth)
    require(isinstance(tokens, dict) and isinstance(tokens.get("id_token"), str)
            and 0 < len(tokens["id_token"]) <= 32768, "OIDC_TOKEN", "Invalid identity token response", 401)
    token = tokens["id_token"]
    try:
        header = jwt.get_unverified_header(token)
    except (jwt.PyJWTError, ValueError, TypeError):
        raise Problem(401, "OIDC_TOKEN", "Invalid identity token header") from None
    require(isinstance(header.get("alg"), str) and header["alg"] in {"RS256", "ES256"}
            and header["alg"] in config.get("id_token_signing_alg_values_supported", ["RS256", "ES256"]),
            "OIDC_ALGORITHM", "Unsupported identity signature algorithm", 401)
    kid = header.get("kid")
    require((kid is None or isinstance(kid, str) and 0 < len(kid) <= 255) and not header.get("crit"),
            "OIDC_SIGNING_KEY", "Unsupported identity signing key header", 401)

    def matching_keys(jwks):
        return [key for key in jwks["keys"]
                if (kid is None or key.get("kid") == kid) and _key_supports(key, header["alg"])]

    matches = matching_keys(signing_keys(settings, config["jwks_uri"]))
    if not matches and kid is not None:
        matches = matching_keys(signing_keys(settings, config["jwks_uri"], refresh=True))
    require(len(matches) == 1, "OIDC_SIGNING_KEY", "No unique trusted signing key for the identity token", 401)
    try:
        key = jwt.PyJWK.from_dict(matches[0], algorithm=header["alg"]).key
        claims = jwt.decode(token, key=key, algorithms=[header["alg"]], audience=settings.oidc_client_id,
                            issuer=settings.oidc_issuer, leeway=30,
                            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
    except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
        raise Problem(401, "OIDC_TOKEN", "Identity token could not be verified") from None
    require(isinstance(claims["sub"], str) and 0 < len(claims["sub"]) <= 255 and claims["sub"].isascii()
            and all(type(claims[name]) in (int, float) and math.isfinite(claims[name]) for name in ("iat", "exp")),
            "OIDC_CLAIMS", "Invalid identity token claims", 401)
    require(isinstance(claims["nonce"], str) and claims["nonce"].isascii() and isinstance(flow["nonce"], str)
            and secrets.compare_digest(claims["nonce"], flow["nonce"]), "OIDC_NONCE", "Identity nonce did not match", 401)
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    require(len(audiences) == 1 or claims.get("azp") == settings.oidc_client_id, "OIDC_AUDIENCE", "Authorised party mismatch", 401)
    if "azp" in claims:
        require(claims["azp"] == settings.oidc_client_id, "OIDC_AUDIENCE", "Authorised party mismatch", 401)
    if flow.get("reauthenticate_after") is not None:
        authenticated = claims.get("auth_time")
        require(type(authenticated) is int and flow["reauthenticate_after"] - 30 <= authenticated <= time.time() + 30,
                "OIDC_REAUTHENTICATION", "Fresh institutional authentication is required; restart sign-in", 401)
    username_claim = getattr(settings, "oidc_username_claim", "preferred_username")
    email_claim = getattr(settings, "oidc_email_claim", "email")
    selected = {username_claim, email_claim, getattr(settings, "oidc_groups_claim", "groups")}
    if getattr(settings, "oidc_allowed_domains", []):
        selected.add("email_verified")
    if selected - claims.keys() and config.get("userinfo_endpoint") and isinstance(tokens.get("access_token"), str):
        info = userinfo(settings, config["userinfo_endpoint"], tokens["access_token"], claims["sub"])
        email_missing = email_claim not in claims
        for name in selected - claims.keys() - {"email_verified"}:
            if name in info:
                claims[name] = info[name]
        # A verification flag applies only to the email returned beside it, never to a different token email.
        if email_claim in info and info[email_claim] == claims.get(email_claim) and (email_missing or "email_verified" not in claims):
            claims["email_verified"] = info.get("email_verified")
    return claims, token


def userinfo(settings, endpoint, access_token, subject):
    """Optional claims are accepted only from the same provider and exact authenticated subject."""
    try:
        info = _request_json(settings, "get", endpoint, headers={"Authorization": "Bearer " + access_token})
    except (Problem, httpx.HTTPError, ValueError):
        return {}
    return info if isinstance(info, dict) and info.get("sub") == subject else {}


def userinfo_username(settings, endpoint, access_token, subject):
    value = userinfo(settings, endpoint, access_token, subject).get("preferred_username")
    return value if isinstance(value, str) else None


def finish_flow(db, settings, claims, *, user_agent=None, peer=None, correlation_id=None, id_token=None, flow_id=None):
    from .services import accounts
    flow = (db.scalar(select(m.OIDCFlow).where(m.OIDCFlow.id == flow_id).with_for_update())
            if flow_id else None)
    require(flow_id is None or (flow is not None and flow.used and aware(flow.expires_at) > m.now()
                               and flow.verified_identity_id is None and flow.completed_session_id is None),
            "OIDC_STATE", "Sign-in was cancelled or expired; restart sign-in", 401)
    if flow is not None and hasattr(settings, "auth_configuration_revision"):
        require(getattr(flow, "config_revision", None) == settings.auth_configuration_revision,
                "OIDC_CONFIGURATION_CHANGED", "Sign-in settings changed; restart sign-in", 401)
    domains = getattr(settings, "oidc_allowed_domains", [])
    if domains:
        email = claims.get(getattr(settings, "oidc_email_claim", "email"))
        match = re.fullmatch(r"[^@\s]+@([^@\s]+)", email) if isinstance(email, str) and len(email) <= 254 else None
        require(claims.get("email_verified") is True and match is not None
                and match[1].casefold() in {domain.casefold() for domain in domains},
                "OIDC_EMAIL_DOMAIN", "A verified institutional email address is required", 403)
    if hasattr(settings, "institutional_bindings"):
        matches = [binding for binding in settings.institutional_bindings
                   if binding.get("issuer") == settings.oidc_issuer and binding["subject"] == claims["sub"]]
        identity = db.get(m.Identity, matches[0]["identity_id"]) if len(matches) == 1 else None
    else:
        identity = db.scalar(select(m.Identity).where(m.Identity.issuer == settings.oidc_issuer, m.Identity.subject == claims["sub"]))
        username = claims.get("preferred_username")
        if identity is None and username:
            identity = accounts.bind_first_sign_in(db, settings, claims["sub"], username)
        elif identity is not None:
            accounts.adopt(db, settings, identity, username)
    require(identity is not None, "UNPROVISIONED_IDENTITY", "Ask your admin to add your institutional sign-in and give you an access role", 403)
    allowed = allowed_departments(db, identity)
    if flow is not None:
        flow.verified_identity_id = identity.id
        if len(allowed) > 1:
            # Exchange remains consumed; only this browser can redeem its verified result.
            return {"department_required": True}
    values = issue_session(db, identity, None, settings, user_agent=user_agent, peer=peer,
                           correlation_id=correlation_id, id_token=id_token,
                           remember_me=getattr(flow, "remember_me", False) if flow is not None else False)
    if flow is not None:
        flow.completed_session_id = values["session_id"]
    return values


def pending_choice(db, settings, continuation, browser, *, lock=False):
    query = select(m.OIDCFlow).where(m.OIDCFlow.state_hash == digest(continuation))
    flow = db.scalar(query.with_for_update() if lock else query)
    require(flow is not None and flow.used and flow.verified_identity_id is not None
            and flow.completed_session_id is None and aware(flow.expires_at) > m.now()
            and browser and secrets.compare_digest(flow.browser_hash, digest(browser)),
            "OIDC_STATE", "Sign-in was cancelled, completed or expired; restart sign-in", 401)
    require(flow.config_revision == settings.auth_configuration_revision,
            "OIDC_CONFIGURATION_CHANGED", "Sign-in settings changed; restart sign-in", 401)
    identity = db.get(m.Identity, flow.verified_identity_id)
    require(identity is not None, "UNPROVISIONED_IDENTITY", "Your identity is no longer available", 403)
    return flow, identity, allowed_departments(db, identity)


def department_choices(db, settings, continuation, browser):
    flow, _, allowed = pending_choice(db, settings, continuation, browser)
    return {"expires_at": aware(flow.expires_at).isoformat(),
            "departments": [{"id": d, "name": db.get(m.Department, d).name} for d in allowed]}


def choose_department(db, settings, continuation, browser, department_id, *, reauthenticate=False, **session_options):
    """Lock the one-use flow row while redeeming it and issuing the session."""
    flow, identity, _ = pending_choice(db, settings, continuation, browser, lock=True)
    require(not reauthenticate or flow.force_login, "OIDC_REAUTHENTICATION",
            "Fresh institutional authentication is required; restart sign-in", 401)
    values = issue_session(db, identity, department_id, settings, remember_me=flow.remember_me, **session_options)
    flow.completed_session_id = values["session_id"]
    return {**values, "display_name": identity.display_name}
