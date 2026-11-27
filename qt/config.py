"""Configuration, trading-mode safety and risk limits.

Design rules:
* Fail closed: any ambiguity in trading mode or credentials raises ConfigError.
* Paper is the default. Live trading needs THREE deliberate settings plus
  separate live credentials, and is never switchable from the dashboard.
* Risk limits have *approved ceilings* (set by the owner in the environment).
  The dashboard may tighten limits but can never loosen them past the ceilings.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

LIVE_CONFIRMATION_PHRASE = "I AUTHORIZE REAL MONEY TRADING"


class ConfigError(RuntimeError):
    pass


@dataclass
class RiskLimits:
    """Initial experimental settings from the spec. Not proven optimal."""
    starting_capital: float = 100.0
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 5.0
    max_drawdown_pct: float = 10.0
    max_positions: int = 2
    max_position_alloc_pct: float = 50.0
    max_total_alloc_pct: float = 80.0
    consecutive_loss_limit: int = 3
    cooldown_minutes: int = 60
    max_pair_correlation: float = 0.85
    min_order_notional: float = 1.0
    max_spread_bps: float = 25.0
    stale_data_seconds: int = 180
    max_abnormal_move_atr: float = 8.0
    # Profit preservation (downside protection only, never a profit ceiling)
    profit_giveback_enabled: bool = True
    profit_giveback_pct: float = 50.0      # of the session's peak profit
    profit_giveback_min_peak: float = 1.0  # $ peak profit before the rule arms
    # Principal protection (optional)
    principal_protection_enabled: bool = False
    principal_floor: float = 95.0
    principal_profit_risk_fraction: float = 0.25

    # Fields where a larger number means MORE risk (used for ceiling checks)
    RISK_INCREASING = (
        "risk_per_trade_pct", "max_daily_loss_pct", "max_weekly_loss_pct",
        "max_drawdown_pct", "max_positions", "max_position_alloc_pct",
        "max_total_alloc_pct", "consecutive_loss_limit", "max_pair_correlation",
        "max_spread_bps", "stale_data_seconds", "max_abnormal_move_atr",
        "profit_giveback_pct", "principal_profit_risk_fraction",
    )
    RISK_DECREASING = ("cooldown_minutes", "principal_floor")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RiskLimits":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def check_within_ceiling(proposed: RiskLimits, ceiling: RiskLimits) -> list[str]:
    """Return a list of violations where `proposed` is riskier than `ceiling`."""
    errs = []
    for name in RiskLimits.RISK_INCREASING:
        if getattr(proposed, name) > getattr(ceiling, name):
            errs.append(f"{name}={getattr(proposed, name)} exceeds approved {getattr(ceiling, name)}")
    for name in RiskLimits.RISK_DECREASING:
        if getattr(proposed, name) < getattr(ceiling, name):
            if name == "principal_floor" and not proposed.principal_protection_enabled:
                continue
            errs.append(f"{name}={getattr(proposed, name)} below approved {getattr(ceiling, name)}")
    if proposed.starting_capital != ceiling.starting_capital:
        errs.append("starting_capital cannot be changed from the dashboard")
    return errs


@dataclass
class Settings:
    mode: str = "paper"
    api_key: str = ""
    api_secret: str = ""
    data_feed: str = "iex"          # free plan real-time feed
    research_feed: str = "sip"      # free plan allows SIP history older than 15 min
    account_type: str = "cash"      # cash: settled funds only (T+1). margin_1x: no leverage, proceeds reusable
    data_dir: Path = Path("data")
    db_path: Path = Path("data/qt.db")
    dashboard_user: str = "admin"
    dashboard_password_hash: str = ""
    session_secret: str = ""
    allow_crypto: bool = False       # 24/7 crypto execution needs separate approval
    allow_overnight: bool = False    # swing holding needs separate approval
    auto_approve_validated: bool = False  # owner opt-in: strategies that pass validation trade without a tap
    # owner-approved EXPERIMENTAL strategies: did NOT pass validation, trade paper anyway under every guardian
    # limit with mandatory stops; labelled unvalidated everywhere. Exact strategy names, e.g. "S_52w_high".
    experimental_strategies: tuple = ()
    # PAPER ONLY, owner request 2026-11-27: if no trade opened by FORCE_TRADE_AFTER_MIN minutes after the open,
    # take the best-ranked opportunity anyway (stop, guardian limits, $100 budget, flattened by the close).
    # Recorded under strategy "FORCED_daily_min" so it never mixes with research-backed statistics.
    min_daily_trades: int = 0
    force_trade_after_min: int = 45
    notify_level: str = "important"  # all | important | daily | off
    telegram_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_to: str = ""
    risk_ceiling: RiskLimits = field(default_factory=RiskLimits)

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    def redacted(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in (
            "api_key", "api_secret", "dashboard_password_hash", "session_secret",
            "telegram_token", "smtp_password")}
        d["api_key_set"] = bool(self.api_key)
        d["risk_ceiling"] = self.risk_ceiling.to_dict()
        d["data_dir"] = str(self.data_dir)
        d["db_path"] = str(self.db_path)
        return d


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"Ambiguous boolean value: {v!r}")


def load_settings(env: dict | None = None, require_credentials: bool = True) -> Settings:
    """Build Settings from environment variables. Fails closed."""
    env = dict(os.environ if env is None else env)
    mode = env.get("TRADING_MODE", "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise ConfigError(f"TRADING_MODE must be 'paper' or 'live', got {mode!r}")

    if mode == "paper":
        key = env.get("ALPACA_PAPER_API_KEY", "")
        secret = env.get("ALPACA_PAPER_API_SECRET", "")
        if env.get("ALPACA_LIVE_API_KEY") and env.get("ALPACA_LIVE_API_KEY") == key:
            raise ConfigError("Paper and live keys are identical; refusing to start")
        if key.startswith("AK"):
            raise ConfigError("ALPACA_PAPER_API_KEY looks like a LIVE key (AK...). Refusing.")
    else:
        if not _bool(env.get("LIVE_TRADING_ENABLED"), False):
            raise ConfigError("TRADING_MODE=live but LIVE_TRADING_ENABLED is not true")
        if env.get("LIVE_TRADING_CONFIRMATION", "") != LIVE_CONFIRMATION_PHRASE:
            raise ConfigError("Live trading confirmation phrase missing or wrong")
        key = env.get("ALPACA_LIVE_API_KEY", "")
        secret = env.get("ALPACA_LIVE_API_SECRET", "")
        if key and key == env.get("ALPACA_PAPER_API_KEY"):
            raise ConfigError("Live key equals paper key; refusing")
        if key.startswith("PK"):
            raise ConfigError("ALPACA_LIVE_API_KEY looks like a PAPER key (PK...). Refusing.")

    if require_credentials and (not key or not secret):
        raise ConfigError(f"Missing Alpaca {mode} credentials")

    account_type = env.get("ACCOUNT_TYPE", "cash").strip().lower()
    if account_type not in ("cash", "margin_1x"):
        raise ConfigError("ACCOUNT_TYPE must be cash or margin_1x")

    data_dir = Path(env.get("QT_DATA_DIR", "data"))
    ceiling = RiskLimits()
    if env.get("QT_RISK_CEILING_JSON"):
        try:
            ceiling = RiskLimits.from_dict(json.loads(env["QT_RISK_CEILING_JSON"]))
        except Exception as e:  # noqa: BLE001
            raise ConfigError(f"QT_RISK_CEILING_JSON invalid: {e}") from e
    if ceiling.starting_capital <= 0:
        raise ConfigError("starting_capital must be positive")

    s = Settings(
        mode=mode, api_key=key, api_secret=secret,
        data_feed=env.get("ALPACA_DATA_FEED", "iex").lower(),
        research_feed=env.get("ALPACA_RESEARCH_FEED", "sip").lower(),
        account_type=account_type,
        data_dir=data_dir,
        db_path=Path(env.get("QT_DB_PATH", str(data_dir / "qt.db"))),
        dashboard_user=env.get("DASHBOARD_USER", "admin"),
        dashboard_password_hash=env.get("DASHBOARD_PASSWORD_HASH", ""),
        session_secret=env.get("SESSION_SECRET", ""),
        allow_crypto=_bool(env.get("ALLOW_CRYPTO_EXECUTION"), False),
        allow_overnight=_bool(env.get("ALLOW_OVERNIGHT_HOLDS"), False),
        auto_approve_validated=_bool(env.get("AUTO_APPROVE_VALIDATED"), False),
        min_daily_trades=int(env.get("MIN_DAILY_TRADES", "0") or 0) if mode == "paper" else 0,
        force_trade_after_min=int(env.get("FORCE_TRADE_AFTER_MIN", "45") or 45),
        experimental_strategies=tuple(x.strip() for x in env.get("EXPERIMENTAL_STRATEGIES", "").split(",") if x.strip()),
        notify_level=env.get("NOTIFY_LEVEL", "important").lower(),
        telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        smtp_host=env.get("SMTP_HOST", ""),
        smtp_port=int(env.get("SMTP_PORT", "587") or 587),
        smtp_user=env.get("SMTP_USER", ""),
        smtp_password=env.get("SMTP_PASSWORD", ""),
        email_to=env.get("NOTIFY_EMAIL_TO", ""),
        risk_ceiling=ceiling,
    )
    if s.notify_level not in ("all", "important", "daily", "off"):
        raise ConfigError("NOTIFY_LEVEL must be all|important|daily|off")
    return s
