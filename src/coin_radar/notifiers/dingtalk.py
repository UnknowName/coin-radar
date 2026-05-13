from __future__ import annotations

import hashlib
import hmac
import base64
import time
import urllib.parse
import logging

import aiohttp
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from coin_radar.config.models import DingTalkConfig
from coin_radar.notifiers.formatter import Signal, format_signal

logger = logging.getLogger(__name__)

# aiohttp 网络异常基类，用于重试判断
_NETWORK_EXCEPTIONS = (
    aiohttp.ClientError,
    aiohttp.ClientConnectionError,
    aiohttp.ClientPayloadError,
)


def _generate_sign(secret: str, timestamp: int) -> str:
    """钉钉自定义机器人签名算法：HmacSHA256 + Base64 + URL编码"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return urllib.parse.quote_plus(sign)


def _build_signed_url(webhook_url: str, secret: str) -> str:
    """根据 secret 生成带签名参数的完整 Webhook URL"""
    timestamp = int(time.time() * 1000)
    sign = _generate_sign(secret, timestamp)
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}timestamp={timestamp}&sign={sign}"


class DingTalkNotifier:
    """钉钉消息推送器，支持签名认证、@指定人员、失败重试"""

    def __init__(self, config: DingTalkConfig) -> None:
        self._webhook_url = config.webhook_url
        self._secret = config.secret
        self._at_mobiles = config.at_mobiles

    def _build_payload(self, title: str, text: str) -> dict:
        """构建钉钉消息体，包含 @ 人员信息"""
        at_text = text
        if self._at_mobiles:
            # 在消息末尾追加 @ 手机号，钉钉要求 @ 文本与 atMobiles 匹配
            at_text += "\n\n" + " ".join(f"@{m}" for m in self._at_mobiles)

        return {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": at_text},
            "at": {
                "atMobiles": self._at_mobiles,
                "isAtAll": False,
            },
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type(_NETWORK_EXCEPTIONS),
        before_sleep=lambda rs: logger.warning(
            "钉钉推送第%d次失败，%ds后重试: %s",
            rs.attempt_number,
            rs.next_action.sleep if rs.next_action else 0,
            rs.outcome.exception(),
        ),
    )
    async def _post(self, url: str, payload: dict) -> dict:
        """发送 POST 请求，网络错误时自动重试（最多3次，指数退避）"""
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                result = await resp.json()
                if result.get("errcode") != 0:
                    logger.error("钉钉推送业务错误: %s", result)
                return result

    async def send_signal(self, signal: Signal) -> dict:
        """格式化信号并发送钉钉消息"""
        title, text = format_signal(signal)
        return await self.send(title, text)

    async def send(self, title: str, text: str) -> dict:
        """发送 Markdown 消息到钉钉"""
        if not self._webhook_url:
            logger.warning("钉钉 Webhook URL 未配置，跳过推送")
            return {"errcode": -1, "errmsg": "webhook_url not configured"}

        url = _build_signed_url(self._webhook_url, self._secret) if self._secret else self._webhook_url
        payload = self._build_payload(title, text)
        return await self._post(url, payload)
