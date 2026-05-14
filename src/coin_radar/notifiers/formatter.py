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
    return "⭐High Priority" if priority == "high" else ""


def _z_score_text(signal: Signal) -> str:
    if signal.z_score is None:
        return ""
    parts = [f"**z-score**: {_fmt_num(signal.z_score)}"]
    if signal.direction:
        parts.append(f"({signal.direction})")
    return " | ".join(parts)


def format_signal(signal: Signal) -> tuple[str, str]:
    """Format Signal to DingTalk Markdown message, return (title, text)"""
    badge = _priority_badge(signal.priority)
    header = f"### 🚀 [{signal.module}] {signal.symbol}"
    if badge:
        header += f" {badge}"

    # Line 1: score | z-score | speculation
    line1_parts = [f"**Score**: {signal.score:.0f}"]
    z_text = _z_score_text(signal)
    if z_text:
        line1_parts.append(z_text)
    if signal.speculation_label:
        line1_parts.append(f"**Speculation**: {signal.speculation_label}")

    lines = [header, " | ".join(line1_parts), "---"]

    # Price and change line
    price_line_parts = []
    if signal.price is not None:
        price_line_parts.append(f"**Price**: {_fmt_price(signal.price)}")
    if signal.change_24h is not None:
        price_line_parts.append(f"**24h Change**: {_fmt_pct(signal.change_24h)}")
    if price_line_parts:
        lines.append(" | ".join(price_line_parts))

    # Volume line
    vol_line_parts = []
    if signal.volume is not None:
        vol_line_parts.append(f"**Volume**: {_fmt_int(int(signal.volume))} {signal.symbol}")
    if signal.volume_1h_multiple is not None:
        vol_line_parts.append(f"**1h Multiple**: {_fmt_num(signal.volume_1h_multiple)}x")
    if signal.volume_24h_multiple is not None:
        vol_line_parts.append(f"**24h Multiple**: {_fmt_num(signal.volume_24h_multiple)}x")
    if vol_line_parts:
        lines.append(" | ".join(vol_line_parts))

    # OI and RSI line
    oi_rsi_parts = []
    if signal.open_interest is not None:
        oi_rsi_parts.append(f"**OI**: {_fmt_int(int(signal.open_interest))}")
    if signal.rsi_14 is not None:
        oi_rsi_parts.append(f"**RSI(14)**: {_fmt_num(signal.rsi_14)}")
    if oi_rsi_parts:
        lines.append(" | ".join(oi_rsi_parts))

    # Resistance and support line
    level_parts = []
    if signal.resistance is not None:
        level_parts.append(f"**Resistance**: {_fmt_price(signal.resistance)}")
    if signal.support is not None:
        level_parts.append(f"**Support**: {_fmt_price(signal.support)}")
    if level_parts:
        lines.append(" | ".join(level_parts))

    lines.append("---")

    # Ratio details line
    if signal.ratio_current is not None:
        ratio_text = f"**Ratio Details**: current {_fmt_num(signal.ratio_current)}"
        if signal.ratio_mean is not None:
            ratio_text += f" | mean {_fmt_num(signal.ratio_mean)}"
            if signal.ratio_std is not None:
                ratio_text += f" ± {_fmt_num(signal.ratio_std)}"
        lines.append(ratio_text)

    # Sample info line
    if signal.sample_count is not None:
        sample_text = f"**Samples**: {_fmt_int(signal.sample_count)}"
        if signal.sample_duration_hours is not None:
            sample_text += f" / {signal.sample_duration_hours / 24:.1f} days"
        lines.append(sample_text)

    # Coinglass link
    lines.append(f"\n[Coinglass](https://www.coinglass.com/zh/{signal.symbol})")

    title = f"[{signal.module}] {signal.symbol}"
    text = "\n\n".join(lines)
    return title, text
