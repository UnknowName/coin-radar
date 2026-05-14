from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExchangeConfig:
    name: str = "binance"
    proxy: str | None = None
    timeout: int = 30


@dataclass
class AltcoinWeights:
    silent_accumulation: float = 0.25
    cvd_anomaly: float = 0.20
    oi_price_stable: float = 0.20
    whale_divergence: float = 0.20
    funding_divergence: float = 0.10
    liquidity_thinning: float = 0.05


@dataclass
class MonitorConfig:
    weights: AltcoinWeights = field(default_factory=AltcoinWeights)
    score_threshold: float = 60.0
    high_priority_threshold: float = 80.0
    dedup_minutes: int = 30
    z_score_threshold: float = 3.0
    baseline_hours: int = 12


@dataclass
class DingTalkConfig:
    client_id: str = ""
    client_secret: str = ""
    robot_code: str = ""
    open_conversation_id: str = ""
    webhook_url: str = ""
    at_user_ids: list[str] = field(default_factory=list)
    at_all: bool = False


@dataclass
class FilterConfig:
    cooldown_minutes: int = 120
    top_n: int = 10


@dataclass
class AppConfig:
    exchanges: list[ExchangeConfig] = field(
        default_factory=lambda: [ExchangeConfig(name="binance")]
    )
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    dingtalk: DingTalkConfig = field(default_factory=DingTalkConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)

    @property
    def exchange_names(self) -> list[str]:
        return [e.name for e in self.exchanges]
