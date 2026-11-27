"""Research universes. The LIVE scanner discovers assets dynamically from Alpaca;
these fixed lists are only the research/backtest datasets.

Known bias: a universe chosen today contains companies that survived and became
popular (survivorship / selection bias). ETFs and index funds reduce, but do
not remove, this bias. Reported in every research report.
"""
CORE_ETFS = ["SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "USO", "SMH", "TLT"]
SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC", "XLRE"]
STOCKS = ["NVDA", "TSLA", "AMD", "AAPL", "META", "MSFT", "AMZN", "GOOG", "PLTR", "JPM", "XOM",
          "UNH", "AVGO", "NFLX", "COIN", "MU", "INTC", "BA", "LLY", "CAT"]
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD"]

# Round 2 (2026-11-27): broad asset-class ETFs for daily/monthly strategies (O-S). Liquid, long-lived funds,
# which keeps survivorship bias lower than a stock list.
ASSET_ETFS = ["EFA", "EEM", "VGK", "EWJ", "IEF", "SHY", "LQD", "HYG", "TIP", "VNQ", "DBC", "XBI", "KRE", "GDX"]
INDEX_ETFS = ["SPY", "QQQ", "IWM", "DIA"]
# trend/rotation set: one fund per major asset class (equity regions, bonds, credit, real estate, commodities)
ASSET_CLASS_SET = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VGK", "EWJ", "TLT", "IEF", "LQD", "HYG", "TIP", "VNQ",
                   "GLD", "SLV", "DBC"]

INTRADAY_UNIVERSE = CORE_ETFS + SECTOR_ETFS + STOCKS
LEGACY_DAILY = CORE_ETFS + SECTOR_ETFS + STOCKS          # universe of round-1 daily strategies (E, M)
DAILY_UNIVERSE = list(dict.fromkeys(LEGACY_DAILY + ASSET_ETFS))

SECTOR_OF = {
    "NVDA": "XLK", "AMD": "XLK", "AAPL": "XLK", "MSFT": "XLK", "AVGO": "XLK", "MU": "XLK", "INTC": "XLK",
    "PLTR": "XLK", "META": "XLC", "GOOG": "XLC", "NFLX": "XLC", "AMZN": "XLY", "TSLA": "XLY",
    "JPM": "XLF", "COIN": "XLF", "XOM": "XLE", "UNH": "XLV", "LLY": "XLV", "BA": "XLI", "CAT": "XLI",
}
