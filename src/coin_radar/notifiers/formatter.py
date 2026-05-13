from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    module: str
    symbol: str
    score: float
    priority: str = "normal"
    z_score: float | None = None
    direction: str | None = None
    speculation_label: str | None = None
    price: float | None = None
    change_24h: float | None = None
    volume: float | None = None
    volume_1h_multiple: float | None = None
    volume_24h_multiple: float | None = None
    open_interest: float | None = None
    rsi_14: float | None = None
    resistance: float | None = None
    support: float | None = None
    ratio_current: float | None = None
    ratio_mean: float | None = None
    ratio_std: float | None = None
    sample_count: int | None = None
    sample_duration_hours: float | None = None
    details: dict | None = None


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 1:
        return f"${value:,.2f}"
    return f"${value:.6f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def _fmt_num(value: float | None, decimal: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimal}f}"


def _fmt_int(value: int | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:,}"


def _priority_badge(priority: str) -> str:
    return "⭐高优先级" if priority == "high" else ""


def _z_score_text(signal: Signal) -> str:
    if signal.z_score is None:
        return ""
    parts = [f"**z-score**: {_fmt_num(signal.z_score)}"]
    if signal.direction:
        parts.append(f"({signal.direction})")
    return " | ".join(parts)


def format_signal(signal: Signal) -> tuple[str, str]:
    """将 Signal 格式化为钉钉 Markdown 消息，返回 (title, text)"""
    badge = _priority_badge(signal.priority)
    header = f"### 🚀 [{signal.module}] {signal.symbol}"
    if badge:
        header += f" {badge}"

    # 第一行：评分 | z-score | 投机度
    line1_parts = [f"**评分**: {signal.score:.0f}分"]
    z_text = _z_score_text(signal)
    if z_text:
        line1_parts.append(z_text)
    if signal.speculation_label:
        line1_parts.append(f"**投机度**: {signal.speculation_label}")

    lines = [header, " | ".join(line1_parts), "---"]

    # 价格与涨跌行
    price_line_parts = []
    if signal.price is not None:
        price_line_parts.append(f"**价格**: {_fmt_price(signal.price)}")
    if signal.change_24h is not None:
        price_line_parts.append(f"**24h涨跌**: {_fmt_pct(signal.change_24h)}")
    if price_line_parts:
        lines.append(" | ".join(price_line_parts))

    # 成交量行
    vol_line_parts = []
    if signal.volume is not None:
        vol_line_parts.append(f"**成交量**: {_fmt_int(int(signal.volume))} {signal.symbol}")
    if signal.volume_1h_multiple is not None:
        vol_line_parts.append(f"**1h倍数**: {_fmt_num(signal.volume_1h_multiple)}x")
    if signal.volume_24h_multiple is not None:
        vol_line_parts.append(f"**24h倍数**: {_fmt_num(signal.volume_24h_multiple)}x")
    if vol_line_parts:
        lines.append(" | ".join(vol_line_parts))

    # OI 与 RSI 行
    oi_rsi_parts = []
    if signal.open_interest is not None:
        oi_rsi_parts.append(f"**OI**: {_fmt_int(int(signal.open_interest))}")
    if signal.rsi_14 is not None:
        oi_rsi_parts.append(f"**RSI(14)**: {_fmt_num(signal.rsi_14)}")
    if oi_rsi_parts:
        lines.append(" | ".join(oi_rsi_parts))

    # 阻力位与支撑位行
    level_parts = []
    if signal.resistance is not None:
        level_parts.append(f"**阻力位**: {_fmt_price(signal.resistance)}")
    if signal.support is not None:
        level_parts.append(f"**支撑位**: {_fmt_price(signal.support)}")
    if level_parts:
        lines.append(" | ".join(level_parts))

    lines.append("---")

    # Ratio 详情行
    if signal.ratio_current is not None:
        ratio_text = f"**Ratio详情**: 当前 {_fmt_num(signal.ratio_current)}"
        if signal.ratio_mean is not None:
            ratio_text += f" | 均值 {_fmt_num(signal.ratio_mean)}"
            if signal.ratio_std is not None:
                ratio_text += f" ± {_fmt_num(signal.ratio_std)}"
        lines.append(ratio_text)

    # 样本信息行
    if signal.sample_count is not None:
        sample_text = f"**样本**: {_fmt_int(signal.sample_count)}个"
        if signal.sample_duration_hours is not None:
            sample_text += f" / {signal.sample_duration_hours / 24:.1f}天"
        lines.append(sample_text)

    # Coinglass 链接
    lines.append(f"\n[Coinglass](https://www.coinglass.com/zh/{signal.symbol})")

    title = f"[{signal.module}] {signal.symbol}"
    text = "\n\n".join(lines)
    return title, text
