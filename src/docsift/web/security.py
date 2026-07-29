"""CSRF для POST-форм. Double submit cookie на stdlib, без новых зависимостей."""

from __future__ import annotations

import hmac
import os
import secrets
from hashlib import sha256

from fastapi import HTTPException, Request, Response, status

CSRF_COOKIE = "docsift_csrf"
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"

_SECRET = os.environ.get("DOCSIFT_WEB_SECRET", "").encode() or secrets.token_bytes(32)


def _sign(raw: str) -> str:
    return hmac.new(_SECRET, raw.encode(), sha256).hexdigest()[:32]


def _equal(left: str, right: str) -> bool:
    """Сравнение за постоянное время, устойчивое к любому вводу.

    hmac.compare_digest на str работает только с ASCII и на кириллице кидает
    TypeError. Токен приходит из формы, то есть от кого угодно: без перевода в
    байты подделанный токен с не-ASCII символами роняет обработчик в 500
    вместо честного 403.
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def issue_token(response: Response, request: Request) -> str:
    """Возвращает токен и, если нужно, ставит cookie."""
    existing = request.cookies.get(CSRF_COOKIE)
    if existing and _verify_format(existing):
        return existing
    raw = secrets.token_urlsafe(16)
    token = f"{raw}.{_sign(raw)}"
    response.set_cookie(
        CSRF_COOKIE, token, httponly=False, samesite="lax", secure=False, path="/"
    )
    return token


def _verify_format(token: str) -> bool:
    raw, _, signature = token.partition(".")
    return bool(raw and signature) and _equal(signature, _sign(raw))


def ensure_token(request: Request) -> str:
    """Токен текущего запроса: из cookie либо новый.

    Результат кладётся в request.state.csrf_token — шаблоны читают его оттуда.
    """
    cached = getattr(request.state, "csrf_token", None)
    if cached:
        return str(cached)
    existing = request.cookies.get(CSRF_COOKIE)
    if existing and _verify_format(existing):
        request.state.csrf_token = existing
        request.state.csrf_token_is_new = False
        return existing
    raw = secrets.token_urlsafe(16)
    token = f"{raw}.{_sign(raw)}"
    request.state.csrf_token = token
    request.state.csrf_token_is_new = True
    return token


def attach_csrf_cookie(request: Request, response: Response) -> None:
    """Ставит cookie, если в этом запросе был выпущен новый токен."""
    if not getattr(request.state, "csrf_token_is_new", False):
        return
    response.set_cookie(
        CSRF_COOKIE,
        request.state.csrf_token,
        httponly=False,
        samesite="lax",
        secure=False,
        path="/",
    )
    request.state.csrf_token_is_new = False


async def verify_csrf(request: Request) -> None:
    """Зависимость FastAPI для всех POST-роутов веб-интерфейса."""
    cookie = request.cookies.get(CSRF_COOKIE, "")
    sent = request.headers.get(CSRF_HEADER, "")
    if not sent:
        form = await request.form()
        sent = str(form.get(CSRF_FIELD, ""))
    if not cookie or not sent or not _equal(cookie, sent) or not _verify_format(cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF-токен недействителен")
