from __future__ import annotations

import os
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

from coin_radar.config.models import (
    AltcoinWeights,
    AppConfig,
    DingTalkConfig,
    ExchangeConfig,
    FilterConfig,
    MonitorConfig,
)

_KEY_ENV = "COIN_RADAR_CRYPTO_KEY"
_KEY_FILE = Path.home() / ".coin_radar" / ".key"


def _get_or_create_key() -> bytes:
    key = os.environ.get(_KEY_ENV)
    if key:
        return key.encode()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    return key


def encrypt_value(plaintext: str) -> str:
    f = Fernet(_get_or_create_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    f = Fernet(_get_or_create_key())
    return f.decrypt(ciphertext.encode()).decode()


def _env_override(data: dict, prefix: str = "COIN_RADAR") -> dict:
    env_mapping = {
        ("exchange", "name"): f"{prefix}_EXCHANGE_NAME",
        ("exchange", "proxy"): f"{prefix}_EXCHANGE_PROXY",
        ("dingtalk", "webhook_url"): f"{prefix}_DINGTALK_WEBHOOK",
        ("dingtalk", "secret"): f"{prefix}_DINGTALK_SECRET",
    }
    for (section, key), env_var in env_mapping.items():
        val = os.environ.get(env_var)
        if val is not None:
            data.setdefault(section, {})[key] = val
    return data


def _build_config(data: dict) -> AppConfig:
    exchange_data = data.get("exchange", {})
    monitor_data = data.get("monitor", {})
    dingtalk_data = data.get("dingtalk", {})
    filter_data = data.get("filter", {})
    weights_data = monitor_data.get("weights", {})

    weights = AltcoinWeights(**weights_data) if weights_data else AltcoinWeights()

    monitor_fields = {k: v for k, v in monitor_data.items() if k != "weights"}
    monitor = MonitorConfig(weights=weights, **monitor_fields)

    dingtalk_raw = DingTalkConfig(**dingtalk_data)
    if dingtalk_raw.secret and dingtalk_raw.secret.startswith("gAAAA"):
        dingtalk_raw.secret = decrypt_value(dingtalk_raw.secret)
    if dingtalk_raw.webhook_url and dingtalk_raw.webhook_url.startswith("gAAAA"):
        dingtalk_raw.webhook_url = decrypt_value(dingtalk_raw.webhook_url)

    return AppConfig(
        exchange=ExchangeConfig(**exchange_data),
        monitor=monitor,
        dingtalk=dingtalk_raw,
        filter=FilterConfig(**filter_data),
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        path = Path(__file__).resolve().parent.parent.parent.parent / "settings.yaml"
    path = Path(path)

    data: dict = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    data = _env_override(data)
    return _build_config(data)
