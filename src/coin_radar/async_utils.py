from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def cleanup_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """安全清理事件循环：取消所有 pending 任务后再关闭

    解决 ccxt Throttler.looper、aiohttp TCPConnector 等后台协程
    在事件循环关闭时未被正确清理导致的资源泄漏问题。
    """
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        logger.exception("Error shutting down async generators")

    # 取消所有尚未完成的任务（如 ccxt Throttler.looper、aiohttp 连接器等）
    pending = asyncio.all_tasks(loop)
    if pending:
        logger.debug("Cancelling %d pending tasks", len(pending))
        for task in pending:
            task.cancel()
        # 等待所有被取消的任务完成，忽略 CancelledError
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    try:
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:
        pass

    loop.close()
