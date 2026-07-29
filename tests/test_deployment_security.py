"""Проверки безопасности развёртывания: docker-compose.yml и .env.example."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _load_compose() -> dict:
    with open(COMPOSE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _read_env_example() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


# ── 1. Порты привязаны к 127.0.0.1 ─────────────────────────────────────────

def test_all_port_mappings_bound_to_loopback() -> None:
    """Каждый маппинг портов в docker-compose.yml начинается с 127.0.0.1."""
    compose = _load_compose()
    violations: list[str] = []
    for service_name, service in compose.get("services", {}).items():
        for port_entry in service.get("ports", []):
            entry = str(port_entry)
            if not entry.startswith("127.0.0.1:"):
                violations.append(f"{service_name}: {entry!r}")
    assert not violations, (
        "Следующие порты не привязаны к 127.0.0.1 и доступны из сети:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ── 2. DOCSIFT_WEB_SECRET в сервисе app ─────────────────────────────────────

def test_app_service_has_web_secret_env() -> None:
    """В сервисе app присутствует переменная окружения DOCSIFT_WEB_SECRET."""
    compose = _load_compose()
    app_env = compose["services"]["app"].get("environment", {})
    assert "DOCSIFT_WEB_SECRET" in app_env, (
        "DOCSIFT_WEB_SECRET отсутствует в environment сервиса app"
    )


# ── 3. DOCSIFT_WEB_SECRET в .env.example ────────────────────────────────────

def test_env_example_has_empty_web_secret() -> None:
    """В .env.example есть ключ DOCSIFT_WEB_SECRET со значением пустая строка."""
    text = _read_env_example()
    match = re.search(r"^DOCSIFT_WEB_SECRET=(.*)$", text, re.MULTILINE)
    assert match is not None, "DOCSIFT_WEB_SECRET не найден в .env.example"
    value = match.group(1).strip()
    assert value == "", (
        f"Значение DOCSIFT_WEB_SECRET в .env.example должно быть пустым, "
        f"получено: {value!r}"
    )


# ── 4. Нет непустых секретов ────────────────────────────────────────────────

_SENSITIVE_KEYS = re.compile(
    r"^(DOCSIFT_WEB_SECRET|.*SECRET.*|.*API_KEY.*|.*PASSWORD.*)$",
    re.IGNORECASE,
)

# Исключение: пароль Postgres для локальной разработки
_KNOWN_EXCEPTIONS = {"POSTGRES_PASSWORD"}


def _check_env_dict(env: dict, source: str) -> list[str]:
    """Вернуть список нарушений: непустые значения у секретных ключей."""
    violations: list[str] = []
    for key, val in env.items():
        if not _SENSITIVE_KEYS.match(key):
            continue
        if key in _KNOWN_EXCEPTIONS:
            continue
        sval = str(val).strip()
        if sval == "" or sval.startswith("${"):
            continue
        violations.append(f"{source}: {key}={sval!r}")
    return violations


def _check_env_text(text: str, source: str) -> list[str]:
    """Вернуть список нарушений в текстовом .env-файле."""
    violations: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not _SENSITIVE_KEYS.match(key):
            continue
        if key in _KNOWN_EXCEPTIONS:
            continue
        val = val.strip()
        if val == "" or val.startswith("${"):
            continue
        violations.append(f"{source}: {key}={val!r}")
    return violations


def test_no_hardcoded_secrets() -> None:
    """Нет непустых значений, похожих на реальные секреты, кроме POSTGRES_PASSWORD."""
    compose = _load_compose()
    violations: list[str] = []

    for svc_name, svc in compose.get("services", {}).items():
        env = svc.get("environment", {})
        if isinstance(env, dict):
            violations.extend(_check_env_dict(env, f"docker-compose:{svc_name}"))

    violations.extend(_check_env_text(_read_env_example(), ".env.example"))

    assert not violations, (
        "Обнаружены потенциально непустые секреты:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
