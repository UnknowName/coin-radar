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
        ("dingtalk", "client_id"): f"{prefix}_DINGTALK_CLIENT_ID",
        ("dingtalk", "client_secret"): f"{prefix}_DINGTALK_CLIENT_SECRET",
        ("dingtalk", "robot_code"): f"{prefix}_DINGTALK_ROBOT_CODE",
        ("dingtalk", "open_conversation_id"): f"{prefix}_DINGTALK_OPEN_CONVERSATION_ID",
    }
    for (section, key), env_var in env_mapping.items():
        val = os.environ.get(env_var)
        if val is not None:
            data.setdefault(section, {})[key] = val
    # Backward compatibility: COIN_RADAR_EXCHANGE_NAME overrides exchanges list
    exchange_name = os.environ.get(f"{prefix}_EXCHANGE_NAME")
    exchange_proxy = os.environ.get(f"{prefix}_EXCHANGE_PROXY")
    if exchange_name or exchange_proxy:
        if "exchanges" not in data:
            data["exchanges"] = [{"name": "binance"}]
        if data["exchanges"]:
            if exchange_name:
                data["exchanges"][0]["name"] = exchange_name
            if exchange_proxy:
                data["exchanges"][0]["proxy"] = exchange_proxy
    return data


def _apply_defaults(exchanges_data: list, defaults: dict) -> list:
    """为每个交易所配置应用全局默认值，支持优先级覆盖机制"""
    result = []
    for exchange in exchanges_data:
        merged = {}
        # 先应用默认值
        for key, value in defaults.items():
            if value is not None:
                merged[key] = value
        # 再应用交易所级别的配置（覆盖默认值）
        for key, value in exchange.items():
            merged[key] = value
        result.append(merged)
    return result


def _build_config(data: dict) -> AppConfig:
    monitor_data = data.get("monitor", {})
    dingtalk_data = data.get("dingtalk", {})
    filter_data = data.get("filter", {})
    weights_data = monitor_data.get("weights", {})

    weights = AltcoinWeights(**weights_data) if weights_data else AltcoinWeights()

    monitor_fields = {k: v for k, v in monitor_data.items() if k != "weights"}
    monitor = MonitorConfig(weights=weights, **monitor_fields)

    dingtalk_raw = DingTalkConfig(**dingtalk_data)
    if dingtalk_raw.client_secret and dingtalk_raw.client_secret.startswith("gAAAA"):
        dingtalk_raw.client_secret = decrypt_value(dingtalk_raw.client_secret)

    # 获取全局默认配置
    defaults = data.get("defaults", {})

    # Parse multi-exchange config, backward compatible with old single-exchange format
    exchanges_data = data.get("exchanges")
    if exchanges_data is None and "exchange" in data:
        exchanges_data = [data["exchange"]]
    if exchanges_data is None:
        exchanges_data = [{"name": "binance"}]
    
    # 应用全局默认值并支持交易所级别覆盖
    if defaults:
        exchanges_data = _apply_defaults(exchanges_data, defaults)
    
    exchanges = [ExchangeConfig(**e) for e in exchanges_data]

    return AppConfig(
        exchanges=exchanges,
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
