from __future__ import annotations

import json
import logging

import aiohttp
import dingtalk_stream
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from coin_radar.config.models import DingTalkConfig
from coin_radar.notifiers.formatter import Signal, format_signal

logger = logging.getLogger(__name__)

_NETWORK_EXCEPTIONS = (
    aiohttp.ClientError,
    aiohttp.ClientConnectionError,
    aiohttp.ClientPayloadError,
)

_SEND_GROUP_MESSAGE_URL = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"


class DingTalkNotifier:
    """DingTalk message notifier, based on dingtalk-stream SDK + enterprise bot OpenAPI"""

    def __init__(self, config: DingTalkConfig) -> None:
        self._config = config
        self._stream_client: dingtalk_stream.DingTalkStreamClient | None = None

    def _ensure_stream_client(self) -> dingtalk_stream.DingTalkStreamClient | None:
        """Lazy initialize DingTalkStreamClient for access_token retrieval and auto-refresh"""
        if self._stream_client is not None:
            return self._stream_client
        if not self._config.client_id or not self._config.client_secret:
            return None
        credential = dingtalk_stream.Credential(
            self._config.client_id,
            self._config.client_secret,
        )
        self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)
        return self._stream_client

    def _get_access_token(self) -> str | None:
        """Get access_token via dingtalk-stream SDK (auto-cached and refreshed)"""
        client = self._ensure_stream_client()
        if client is None:
            return None
        return client.get_access_token()

    @staticmethod
    def _build_msg_param(title: str, text: str) -> str:
        """Build enterprise bot message parameter JSON string"""
        return json.dumps({"title": title, "text": text}, ensure_ascii=False)

    def _build_payload(self, title: str, text: str) -> dict:
        """Build enterprise bot send group message request body"""
        payload = {
            "robotCode": self._config.robot_code or self._config.client_id,
            "openConversationId": self._config.open_conversation_id,
            "msgKey": "sampleMarkdown",
            "msgParam": self._build_msg_param(title, text),
        }
        if self._config.at_user_ids:
            payload["atUserIds"] = self._config.at_user_ids
        if self._config.at_all:
            payload["atAll"] = True
        return payload

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type(_NETWORK_EXCEPTIONS),
        before_sleep=lambda rs: logger.warning(
            "DingTalk push attempt #%d failed, retry in %ds: %s",
            rs.attempt_number,
            rs.next_action.sleep if rs.next_action else 0,
            rs.outcome.exception(),
        ),
    )
    async def _post(self, url: str, headers: dict, payload: dict) -> dict:
        """Send POST request, auto-retry on network errors (max 3 times, exponential backoff)"""
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                return await self._parse_response(resp)

    @staticmethod
    async def _parse_response(resp: aiohttp.ClientResponse) -> dict:
        """Convert OpenAPI response to unified errcode/errmsg format"""
        if resp.status == 200:
            body = await resp.json()
            return {"errcode": 0, "errmsg": "ok", **body}
        try:
            body = await resp.json()
            errmsg = body.get("message", json.dumps(body, ensure_ascii=False))
        except Exception:
            errmsg = await resp.text()
        logger.error("DingTalk push error: HTTP %d - %s", resp.status, errmsg)
        return {"errcode": resp.status, "errmsg": errmsg}

    async def send_signal(self, signal: Signal) -> dict:
        """Format signal and send DingTalk message"""
        title, text = format_signal(signal)
        return await self.send(title, text)

    async def send_signals_batch(self, signals: list[Signal]) -> dict:
        """Batch send multiple signals as a single aggregated message"""
        if not signals:
            return {"errcode": 0, "errmsg": "no signals to send", "sent_count": 0}

        if not self._config.client_id or not self._config.client_secret:
            logger.warning("DingTalk Client ID / Client Secret not configured, skipping batch push")
            return {"errcode": -1, "errmsg": "client_id or client_secret not configured", "sent_count": 0}

        if not self._config.open_conversation_id:
            logger.warning("DingTalk open_conversation_id not configured, skipping batch push")
            return {"errcode": -1, "errmsg": "open_conversation_id not configured", "sent_count": 0}

        access_token = self._get_access_token()
        if not access_token:
            logger.error("Failed to get DingTalk access_token")
            return {"errcode": -1, "errmsg": "failed to get access_token", "sent_count": 0}

        headers = {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": access_token,
        }
        title, text = self._format_batch_signals(signals)
        payload = self._build_payload(title, text)
        result = await self._post(_SEND_GROUP_MESSAGE_URL, headers, payload)
        result["sent_count"] = len(signals)
        return result

    def _format_batch_signals(self, signals: list[Signal]) -> tuple[str, str]:
        """Format multiple signals into a single aggregated message"""
        if len(signals) == 1:
            return format_signal(signals[0])

        # Group by module
        signals_by_module: dict[str, list[Signal]] = {}
        for signal in signals:
            if signal.module not in signals_by_module:
                signals_by_module[signal.module] = []
            signals_by_module[signal.module].append(signal)

        # Build aggregated message
        title = f"📊 批量信号提醒 ({len(signals)} 条)"
        lines = [f"## 📈 批量信号提醒 - 共 {len(signals)} 条信号\n"]

        # Add summary section
        lines.append("### 📋 信号汇总\n")
        summary_lines = []
        for module, module_signals in signals_by_module.items():
            symbols = ", ".join([s.symbol for s in module_signals])
            summary_lines.append(f"- **{module}**: {symbols} ({len(module_signals)}条)")
        lines.append("\n".join(summary_lines))
        lines.append("\n---\n")

        # Add detailed signals (limit to first 10 to avoid message too long)
        max_detailed = 10
        for i, signal in enumerate(signals[:max_detailed], 1):
            _, signal_text = format_signal(signal)
            lines.append(f"### {i}. {signal_text.split(chr(10))[0]}")  # First line only
            lines.append("")

        if len(signals) > max_detailed:
            lines.append(f"\n> ... 还有 {len(signals) - max_detailed} 条信号，详见上方汇总\n")

        lines.append(f"\n[Coinglass](https://www.coinglass.com/zh/)")

        return title, "\n\n".join(lines)

    async def send(self, title: str, text: str) -> dict:
        """Send Markdown message to DingTalk group (Webhook or OpenAPI)"""
        # 优先使用 Webhook 方式（如果配置了）
        if self._config.webhook_url:
            return await self._send_via_webhook(title, text)

        if not self._config.client_id or not self._config.client_secret:
            logger.warning("DingTalk Client ID / Client Secret not configured, skipping push")
            return {"errcode": -1, "errmsg": "client_id or client_secret not configured"}

        if not self._config.open_conversation_id:
            logger.warning("DingTalk open_conversation_id not configured, skipping push")
            return {"errcode": -1, "errmsg": "open_conversation_id not configured"}

        access_token = self._get_access_token()
        if not access_token:
            logger.error("Failed to get DingTalk access_token")
            return {"errcode": -1, "errmsg": "failed to get access_token"}

        headers = {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": access_token,
        }
        payload = self._build_payload(title, text)
        return await self._post(_SEND_GROUP_MESSAGE_URL, headers, payload)

    async def _send_via_webhook(self, title: str, text: str) -> dict:
        # Webhook 推送方式（需求3.4.1兼容）
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": text},
        }
        if self._config.at_all:
            payload["at"] = {"isAtAll": True}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self._config.webhook_url, json=payload) as resp:
                    body = await resp.json()
                    if body.get("errcode") == 0:
                        return {"errcode": 0, "errmsg": "ok"}
                    return {"errcode": body.get("errcode", -1), "errmsg": body.get("errmsg", "unknown")}
        except Exception as e:
            logger.error("Webhook push failed: %s", e)
            return {"errcode": -1, "errmsg": str(e)}
