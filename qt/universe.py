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

INTRADAY_UNIVERSE = CORE_ETFS + SECTOR_ETFS + STOCKS
DAILY_UNIVERSE = CORE_ETFS + SECTOR_ETFS + STOCKS

SECTOR_OF = {
    "NVDA": "XLK", "AMD": "XLK", "AAPL": "XLK", "MSFT": "XLK", "AVGO": "XLK", "MU": "XLK", "INTC": "XLK",
    "PLTR": "XLK", "META": "XLC", "GOOG": "XLC", "NFLX": "XLC", "AMZN": "XLY", "TSLA": "XLY",
    "JPM": "XLF", "COIN": "XLF", "XOM": "XLE", "UNH": "XLV", "LLY": "XLV", "BA": "XLI", "CAT": "XLI",
}
