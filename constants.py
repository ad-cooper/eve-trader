import os
from pathlib import Path

# --- Paths & Cache Files ---
DATA_DIR = "data"
LEDGER_DIR = "ledgers"
OUTPUT_DIR = "ledgers"
PROPOSALS_DIR = "ledgers/ai_proposals"

DUCKDB_PATH = "data/eve_analytics.duckdb"
SQLITE_PATH = "data/latest-sqlite.db"
JEVEASSETS_CSV_PATH = "data/jeveassets_transactions.csv"
BLACKLIST_FILE = "data/temporary_blacklist.json"
STRATEGY_CONFIG_FILE = "data/strategy_config.json"
EMPTY_DUMP_CACHE = "data/.everef_pending_dumps.json"

# ai reasoning
DESK_CONFIG_PATH = Path("services/desk_config.py")
TUNABLE_PROFILE_FIELDS = (
    "vol_pct",
    "min_margin",
    "live_min_margin",
    "min_turnover_m",
    "min_spread_pct",
    "min_dump_freq_pct",
    "max_vwap_ratio_180d",
)
PROPOSALS_DIR = "ledgers/ai_proposals"

SSO_TOKEN_FILE = "ledgers/sso_token.json"
ORDERS_CACHE_FILE = "ledgers/character_orders.json"
ASSETS_CACHE_FILE = "ledgers/character_assets.json"
MARKET_CACHE_FILE = "ledgers/market_prices_cache.json"
PERF_CACHE_FILE = "ledgers/performance_cache.json"
IMPORT_CACHE_FILE = "ledgers/import_suggestions_cache.json"
SNIPER_DEALS_FILE = "ledgers/sniper_deals.json"

WALLET_CACHE_FILE = "data/wallet_cache.json"
WALLET_TX_CACHE_FILE = "data/wallet_transactions_cache.json"
HISTORY_CACHE_FILE = "data/history_cache.json"

# --- EVE Universe, ESI Endpoints & Auth ---
REGION_THE_FORGE = 10000002
JITA_STATION_ID = 60003760
JITA_SYSTEM_ID = 30000142
PERIMETER_SYSTEM_ID = 30000144
JUMP_ORDER_RANGES = {"1", "2", "3", "4", "5", "10", "20", "30", "40"}

EVE_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
EVE_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
ESI_BASE_URL = "https://esi.evetech.net/latest"
BASE_DUMP_URL = "https://data.everef.net/market-history"

ESI_HEADERS = {"User-Agent": "eve-trading-desk-app/1.0 (local trading bot)"}
ESI_CLIENT_HEADERS = {"User-Agent": "EVE-Desk-ESIClient/2.0"}

CLIENT_ID = os.getenv("EVE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("EVE_CLIENT_SECRET", "")
CALLBACK_URL = os.getenv("EVE_CALLBACK_URL", "http://localhost:8000/sso/callback")
SCOPES = " ".join([
    "esi-markets.read_character_orders.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-assets.read_assets.v1",
    "esi-markets.structure_markets.v1",
    "esi-universe.read_structures.v1",
])
# --- Market Fees & Accounting ---
SALES_TAX_RATE = 0.03375          # 3.375% Accounting V
SELL_BROKER_FEE = 0.0123          # 1.23% Broker Relations V in Citadel
TOTAL_EXIT_FEE = SALES_TAX_RATE + SELL_BROKER_FEE  # 4.605%
BUY_SCC_SURCHARGE = 0.0050        # 0.50% SCC Citadel 1-jump surcharge

# --- Desk & Strategy Defaults ---
DESK_ORDER = ("hold", "tactical", "evergreen")
DEFAULT_FALLBACK_BUDGET = 50.0e9
DEFAULT_SCALE_BASE_BUDGET = 50.0e9
DEFAULT_MIN_SCALE_FACTOR = 1.0
DEFAULT_MAX_SCALE_FACTOR = 2.5
DEFAULT_ACTIVE_VERSION = "v2"
AUTOPILOT_BRANCH = "ai-autopilot"