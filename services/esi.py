import os
import json
import time
import base64
import urllib.parse
import requests
import threading
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from services.sso import (
    get_valid_access_token,
    load_sso_tokens
)
from services.storage import read_json, write_json

from constants import (
    ASSETS_CACHE_FILE,
    CALLBACK_URL,
    CLIENT_ID,
    ESI_BASE_URL,
    ESI_HEADERS,
    EVE_AUTH_URL,
    HISTORY_CACHE_FILE,
    ORDERS_CACHE_FILE,
    JITA_STATION_ID,
    JITA_SYSTEM_ID,
    JUMP_ORDER_RANGES,
    PERIMETER_SYSTEM_ID,
    REGION_THE_FORGE,
    SCOPES,
    WALLET_CACHE_FILE,
    WALLET_TX_CACHE_FILE
)

_ESI_SESSION: requests.Session | None = None
_ESI_SESSION_LOCK = threading.Lock()

def _esi_request_json(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    headers: dict | None = None,
    params: dict | None = None,
    json_payload: object = None,
    data: dict | None = None,
    timeout: float | tuple[float, float] = 10.0,
    context: str = "",
    response_type: str = "json",
):
    client = session if session is not None else get_esi_session()
    req_headers = dict(ESI_HEADERS)
    if headers:
        req_headers.update(headers)
    label = context or url
    try:
        res = client.request(
            method=method.upper(),
            url=url,
            headers=req_headers,
            params=params,
            json=json_payload,
            data=data,
            timeout=timeout,
        )
        if res.status_code != 200:
            print(f"[!] ESI {method.upper()} HTTP {res.status_code} ({label}): {res.text[:200]}")
            return None
        if response_type == "text":
            return res.text
        return res.json()
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"[!] ESI {method.upper()} failed ({label}): {exc}")
        return None
    
def create_esi_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=retries)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "EVE-Desk-ESIClient/2.0"})
    return session

def get_esi_session() -> requests.Session:
    """Returns a shared module-level ESI session, initializing it lazily."""
    global _ESI_SESSION
    if _ESI_SESSION is not None:
        return _ESI_SESSION
    with _ESI_SESSION_LOCK:
        if _ESI_SESSION is None:
            _ESI_SESSION = create_esi_session()
    return _ESI_SESSION

def reset_esi_session() -> None:
    """Closes and clears the cached session (useful in tests or after network resets)."""
    global _ESI_SESSION
    with _ESI_SESSION_LOCK:
        if _ESI_SESSION is not None:
            _ESI_SESSION.close()
            _ESI_SESSION = None

def resolve_character_equity(
    orders_cache_paths: tuple[str, ...] = ("ledgers/character_orders.json", "data/character_orders.json"),
    wallet_cache_paths: tuple[str, ...] = ("data/wallet_cache.json", "ledgers/wallet_cache.json"),
    min_valid_equity: float = 1.0e9,
) -> tuple[float, float, float]:
    """Returns (wallet, escrow, total_equity) using live ESI with local cache fallback."""
    wallet = 0.0
    escrow = 0.0

    try:
        wallet = float(fetch_wallet_balance() or 0.0)
        orders = fetch_character_orders() or {}
        escrow = float(orders.get("total_escrow", 0.0))
    except Exception as e:
        print(f"[!] Live ESI equity lookup failed: {e}")

    if escrow <= 0.0:
        for path in orders_cache_paths:
            cached = float((read_json(path) or {}).get("total_escrow", 0.0))
            if cached > 0:
                print(f"[*] Recovered {cached/1e9:.2f}B ISK escrow from local cache ({path})")
                escrow = cached
                break

    if wallet <= 0.0:
        for path in wallet_cache_paths:
            cached = float((read_json(path) or {}).get("balance", 0.0))
            if cached > 0:
                print(f"[*] Recovered {cached/1e9:.2f}B ISK wallet from local cache ({path})")
                wallet = cached
                break

    total_equity = wallet + escrow
    if total_equity > min_valid_equity:
        print(f"[*] Dynamic equity resolved: {wallet/1e9:.2f}B Liquid + {escrow/1e9:.2f}B Escrow = {total_equity/1e9:.2f}B ISK")
    return wallet, escrow, total_equity

def fetch_character_orders():
    tokens = load_sso_tokens()
    if not tokens or not tokens.get("character_id"):
        return None
    token = get_valid_access_token()
    if not token:
        return None
    url = f"{ESI_BASE_URL}/characters/{tokens['character_id']}/orders/"
    res = _esi_request_json(
        "GET",
        url,
        params={"datasource": "tranquility"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if not res:
        return None
    buys, sells = {}, {}
    escrow = 0.0
    for o in res:
        tid = o["type_id"]
        info = {"order_id": o["order_id"], "price": o["price"], "volume_remain": o["volume_remain"], "volume_total": o.get("volume_total", o["volume_remain"]), "escrow": float(o.get("escrow") or (o["price"] * o["volume_remain"]))}
        if o.get("is_buy_order"):
            buys[tid] = info
            escrow += o.get("escrow", o["price"] * o["volume_remain"])
        else:
            sells[tid] = info
    cache = {"buys": buys, "sells": sells, "count": len(res), "total_escrow": escrow}
    with open(ORDERS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
    return cache

_MEM_WALLET_BAL_CACHE: dict = {"character_id": None, "timestamp": 0.0, "balance": 0.0}
_MEM_WALLET_TX_CACHE: dict = {"character_id": None, "timestamp": 0.0, "transactions": []}


def _cached_esi_get(
    mem_cache: dict,
    cache_file: str,
    character_id: int,
    max_age_seconds: int,
    value_field: str,
    default_value,
    fetch_fn,
    force_refresh: bool = False,
):
    """3-tier ESI cache (in-memory -> disk TTL -> live fetch with stale fallback on error/429)."""
    now = time.time()
    if (
        not force_refresh
        and mem_cache.get("character_id") == character_id
        and (now - float(mem_cache.get("timestamp", 0.0))) < max_age_seconds
    ):
        return mem_cache.get(value_field, default_value)

    stale_val = default_value
    cdata = read_json(cache_file)
    if isinstance(cdata, dict) and cdata.get("character_id") == character_id:
        stale_val = cdata.get(value_field, default_value)
        ts = float(cdata.get("timestamp", 0.0))
        if not force_refresh and (now - ts) < max_age_seconds:
            mem_cache.update({"character_id": character_id, "timestamp": ts, value_field: stale_val})
            return stale_val

    fresh_val = fetch_fn()
    if fresh_val is not None:
        mem_cache.update({"character_id": character_id, "timestamp": now, value_field: fresh_val})
        write_json(cache_file, {"character_id": character_id, "timestamp": now, value_field: fresh_val}, indent=None)
        return fresh_val
    return stale_val


def fetch_wallet_balance(force_refresh: bool = False) -> float:
    """Fetches live liquid ISK balance with 300s memory/disk caching and 429 fallback."""
    tokens = load_sso_tokens()
    if not tokens or not tokens.get("character_id"):
        return 0.0
    cid = int(tokens["character_id"])

    def _fetch():
        token = get_valid_access_token()
        if not token:
            return None
        url = f"{ESI_BASE_URL}/characters/{cid}/wallet/"
        res = _esi_request_json(
            "GET",
            url,
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            context=f"wallet_balance cid={cid}",
            response_type="text",
        )
        if res is None:
            return None
        try:
            return float(res.strip())
        except ValueError as exc:
            print(f"[!] Non-numeric wallet balance for cid={cid}: {res[:80]!r} ({exc})")
            return None

    val = _cached_esi_get(
        _MEM_WALLET_BAL_CACHE, WALLET_CACHE_FILE, cid, 300, "balance", 0.0, _fetch, force_refresh=force_refresh
    )
    return float(val or 0.0)


def fetch_wallet_transactions(force_refresh: bool = False, max_age_seconds: int = 300) -> list:
    """Fetches character wallet transactions with a 300s memory + disk cache and stale fallback."""
    tokens = load_sso_tokens()
    if not tokens or not tokens.get("character_id"):
        return []
    cid = int(tokens["character_id"])

    def _fetch():
        token = get_valid_access_token()
        if not token:
            return None
        url = f"{ESI_BASE_URL}/characters/{cid}/wallet/transactions/"
        res = _esi_request_json(
            "GET",
            url,
            params={"datasource": "tranquility"},
            headers={"Authorization": f"Bearer {token}"}
        )
        return res

    return _cached_esi_get(
        _MEM_WALLET_TX_CACHE, WALLET_TX_CACHE_FILE, cid, max_age_seconds, "transactions", [], _fetch, force_refresh=force_refresh
    )

def fetch_character_assets():
    tokens = load_sso_tokens()
    if not tokens or not tokens.get("character_id"):
        return []
    token = get_valid_access_token()
    if not token:
        return []

    char_id = tokens["character_id"]
    all_assets = []
    page = 1
    url = f"{ESI_BASE_URL}/characters/{char_id}/assets/"
    while res := _esi_request_json(
        "GET",
        url,
        params={"datasource": "tranquility", "page": page},
        headers={"Authorization": f"Bearer {token}"},
        context=f"fetch_character_assets() cid={char_id} page={page}",
    ):
        if not isinstance(res, list):
            break
        all_assets.extend(res)
        page += 1
    if not all_assets:
        return []

    jita_containers = {
        a["item_id"] for a in all_assets
        if a.get("location_id") == JITA_STATION_ID and a.get("location_flag") in ["Hangar", "Deliveries"]
    }

    eligible_assets = []
    for a in all_assets:
        if a.get("is_blueprint_copy", False) or a.get("is_singleton", False):
            continue
        loc_id = a.get("location_id")
        loc_flag = a.get("location_flag")

        if (loc_id == JITA_STATION_ID and loc_flag in ["Hangar", "Deliveries"]) or (loc_id in jita_containers):
            eligible_assets.append(a)
        elif loc_id and loc_id >= 1_000_000_000_000 and loc_flag in ["Hangar", "Deliveries"]:
            eligible_assets.append(a)

    with open(ASSETS_CACHE_FILE, "w") as f:
        json.dump(eligible_assets, f, indent=2)

    return eligible_assets

def load_cached_assets(max_age_seconds: int = 300, force: bool = False, *args, **kwargs):
    if force:
        return []
    if os.path.exists(ASSETS_CACHE_FILE):
        import time
        mtime = os.path.getmtime(ASSETS_CACHE_FILE)
        if (time.time() - mtime) > max_age_seconds:
            return []
        with open(ASSETS_CACHE_FILE, "r") as f:
            return json.load(f)

# ==========================================

# ==========================================
# MARKET HISTORY & VOLUME GUARD
# ==========================================

_MEM_HISTORY_CACHE = None
_HISTORY_CACHE_LOCK = threading.Lock()

def _load_history_cache():
    global _MEM_HISTORY_CACHE
    with _HISTORY_CACHE_LOCK:
        if _MEM_HISTORY_CACHE is not None:
            return _MEM_HISTORY_CACHE
        if os.path.exists(HISTORY_CACHE_FILE):
            with open(HISTORY_CACHE_FILE, "r") as f:
                _MEM_HISTORY_CACHE = json.load(f)
                return _MEM_HISTORY_CACHE
        _MEM_HISTORY_CACHE = {}
        return _MEM_HISTORY_CACHE

def fetch_market_history(type_id: int, region_id: int = 10000002) -> list:
    """Fetches Jita daily market history from ESI, cached in-memory and on disk."""
    now = time.time()
    cache = _load_history_cache()

    with _HISTORY_CACHE_LOCK:
        entry = cache.get(str(type_id))
    if entry and (now - entry.get("timestamp", 0) < 43200):
        return entry.get("history", [])

    url = f"{ESI_BASE_URL}/markets/{region_id}/history/"
    history = _esi_request_json(
        "GET",
        url,
        params={"datasource": "tranquility", "type_id": type_id},
        timeout=(3.05, 8.0),
        context=f"fetch_market_history() region={region_id} type_id={type_id}",
    )
    if isinstance(history, list):
        with _HISTORY_CACHE_LOCK:
            cache[str(type_id)] = {"timestamp": now, "history": history}
            snapshot = dict(cache)
        tmp_file = f"{HISTORY_CACHE_FILE}.{threading.get_ident()}.tmp"
        with open(tmp_file, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp_file, HISTORY_CACHE_FILE)
        return history

    return entry.get("history", []) if entry else []

def get_14d_average_volume(type_id: int, region_id: int = 10000002) -> float:
    """Calculates average daily volume over the past 14 days."""
    history = fetch_market_history(type_id, region_id)
    if not history:
        return -1.0  # -1.0 indicates volume data unavailable / network error
    recent = history[-14:]
    if not recent:
        return 0.0
    total_vol = sum(day.get("volume", 0) for day in recent)
    return round(total_vol / len(recent), 2)

def fetch_character_locations_stock(
    character_id: int | None,
    access_token: str | None,
    location_ids: list[int],
) -> dict[int, dict[int, dict]]:
    """
    Fetches a character's hangar assets (walking nested container chains) and active sell orders
    across multiple `location_ids` in a single ESI pass.
    Returns {location_id: {type_id: {'hangar_qty': int, 'sell_qty': int, 'my_lowest_sell': float|None}}}.
    """
    target_locs = {int(loc) for loc in location_ids if loc}
    result: dict[int, dict[int, dict]] = {loc: {} for loc in target_locs}
    if not character_id or not access_token or not target_locs:
        return result

    headers = {"Authorization": f"Bearer {access_token}"}
    all_assets = []
    page = 1
    assets_url = f"{ESI_BASE_URL}/characters/{character_id}/assets/"
    while rows := _esi_request_json(
        "GET",
        assets_url,
        headers=headers,
        params={"datasource": "tranquility", "page": page},
        timeout=10,
        context=f"locations_stock_assets cid={character_id} page={page}",
    ):
        if not isinstance(rows, list):
            break
        all_assets.extend(rows)
        if len(rows) < 1000:
            break
        page += 1

    parent_map = {
        int(a["item_id"]): int(a["location_id"])
        for a in all_assets
        if "item_id" in a and "location_id" in a
    }

    def resolve_target_loc(loc_id: int) -> int | None:
        seen = set()
        cur = loc_id
        while cur and cur not in seen:
            if cur in target_locs:
                return cur
            seen.add(cur)
            cur = parent_map.get(cur)
        return None

    for a in all_assets:
        matched_loc = resolve_target_loc(int(a.get("location_id") or 0))
        if matched_loc is None:
            continue
        flag = str(a.get("location_flag") or "")
        if flag not in ("Hangar", "Unlocked", "AutoFit", "Cargo", "FleetHangar", "ShipHangar"):
            continue
        tid = int(a["type_id"])
        qty = int(a.get("quantity", 1))
        entry = result[matched_loc].setdefault(tid, {"hangar_qty": 0, "sell_qty": 0, "my_lowest_sell": None})
        entry["hangar_qty"] += qty

    orders_url = f"{ESI_BASE_URL}/characters/{character_id}/orders/"
    orders_data = _esi_request_json(
        "GET",
        orders_url,
        headers=headers,
        params={"datasource": "tranquility"},
        timeout=10,
        context=f"locations_stock_orders cid={character_id}",
    )
    if isinstance(orders_data, list):
        try:
            for o in orders_data:
                loc = int(o.get("location_id") or 0)
                if loc not in target_locs or o.get("is_buy_order"):
                    continue
                tid = int(o["type_id"])
                vol = int(o.get("volume_remain", 0))
                price = float(o.get("price", 0.0))
                entry = result[loc].setdefault(tid, {"hangar_qty": 0, "sell_qty": 0, "my_lowest_sell": None})
                entry["sell_qty"] += vol
                if price > 0 and (entry["my_lowest_sell"] is None or price < entry["my_lowest_sell"]):
                    entry["my_lowest_sell"] = price
        except (KeyError, TypeError, ValueError) as exc:
            print(f"[!] Malformed character orders payload in locations_stock for cid={character_id}: {exc}")

    return result

def fetch_structure_order_book(structure_id: int, access_token: str) -> tuple[dict, list[dict]]:
    """
    Downloads all pages of a player structure's market via ESI and returns:
      (aggregated_book_by_type_id, raw_orders_list)
    """
    client = get_esi_session()
    req_headers = dict(ESI_HEADERS)
    req_headers["Authorization"] = f"Bearer {access_token}"
    url = f"{ESI_BASE_URL}/markets/structures/{structure_id}/"

    first = client.get(
        url,
        headers=req_headers,
        params={"datasource": "tranquility", "page": 1},
        timeout=10,
    )
    first.raise_for_status()
    total_pages = int(first.headers.get("X-Pages", 1))
    all_orders = list(first.json())

    if total_pages > 1:
        def get_page(p: int):
            rows = _esi_request_json(
                "GET",
                url,
                session=client,
                headers=req_headers,
                params={"datasource": "tranquility", "page": p},
                timeout=10,
                context=f"structure_book sid={structure_id} page={p}",
            )
            return rows if isinstance(rows, list) else []

        with ThreadPoolExecutor(max_workers=8) as ex:
            for rows in ex.map(get_page, range(2, total_pages + 1)):
                all_orders.extend(rows)

    book = {}
    for o in all_orders:
        tid = int(o["type_id"])
        price = float(o["price"])
        vol = int(o["volume_remain"])
        entry = book.setdefault(tid, {"lowest_ask": None, "sell_vol": 0, "top_buy": None, "daily_vol": None})
        if o.get("is_buy_order"):
            if entry["top_buy"] is None or price > entry["top_buy"]:
                entry["top_buy"] = price
        else:
            entry["sell_vol"] += vol
            if entry["lowest_ask"] is None or price < entry["lowest_ask"]:
                entry["lowest_ask"] = price
    return book, all_orders

def extract_top_buy_for_station(
    orders: list[dict],
    station_id: int = JITA_STATION_ID,
    system_id: int = JITA_SYSTEM_ID,
    neighbor_system_ids: set[int] | None = None,
) -> float | None:
    """
    Returns the highest competing buy order price reachable from `station_id`.
    Public ESI /markets/{region_id}/orders/ only returns NPC station orders
    (Upwell structure orders are never returned on regional market endpoints).
    """
    if not orders:
        return None
    neighbors = neighbor_system_ids if neighbor_system_ids is not None else (
        {PERIMETER_SYSTEM_ID} if system_id == JITA_SYSTEM_ID else set()
    )
    competing: list[float] = []
    for o in orders:
        if not o.get("is_buy_order", True):
            continue
        loc = o.get("location_id")
        sys_id = o.get("system_id")
        rng = str(o.get("range", "")).strip().lower()

        if loc == station_id:
            competing.append(float(o["price"]))
        elif rng == "region":
            competing.append(float(o["price"]))
        elif sys_id == system_id and rng != "station":
            competing.append(float(o["price"]))
        elif sys_id in neighbors and rng in JUMP_ORDER_RANGES:
            competing.append(float(o["price"]))

    return max(competing) if competing else None

def extract_lowest_sell_for_station(
    orders: list[dict],
    station_id: int = JITA_STATION_ID,
) -> float | None:
    """Returns the lowest sell order price at `station_id` from a list of market order dicts."""
    if not orders:
        return None
    prices = [
        float(o["price"])
        for o in orders
        if o.get("location_id") == station_id and not o.get("is_buy_order", True)
    ]
    return min(prices) if prices else None


def _fetch_region_type_orders(
    type_id: int,
    session: requests.Session | None = None,
    order_type: str = "all",
    region_id: int = REGION_THE_FORGE,
    timeout: float = 8.0,
) -> list[dict]:
    url = f"{ESI_BASE_URL}/markets/{region_id}/orders/"
    data = _esi_request_json(
        "GET",
        url,
        session=session,
        params={"datasource": "tranquility", "order_type": order_type, "type_id": type_id},
        timeout=timeout,
        context=f"region={region_id} type_id={type_id} order_type={order_type}",
    )
    return data if isinstance(data, list) else []

def fetch_station_book_top(
    type_id: int,
    session: requests.Session | None = None,
    station_id: int = JITA_STATION_ID,
    region_id: int = REGION_THE_FORGE,
) -> tuple[int, float | None, float | None]:
    """Fetches (type_id, top_buy, lowest_sell) for `station_id` in a single order_type=all call."""
    orders = _fetch_region_type_orders(
        type_id=type_id,
        session=session,
        order_type="all",
        region_id=region_id,
    )
    if not orders:
        return type_id, None, None
    top_buy = extract_top_buy_for_station(orders=orders, station_id=station_id)
    lowest_sell = extract_lowest_sell_for_station(orders=orders, station_id=station_id)
    return type_id, top_buy, lowest_sell

def fetch_universe_names(ids: list[int]) -> dict[int, str]:
    """Resolves EVE universe IDs to names via POST /universe/names/."""
    clean_ids = sorted({int(i) for i in (ids or []) if i})
    if not clean_ids:
        return {}
    url = f"{ESI_BASE_URL}/universe/names/"
    data = _esi_request_json(
        "POST",
        url,
        params={"datasource": "tranquility"},
        json_payload=clean_ids,
        timeout=6,
        context=f"universe/names count={len(clean_ids)}",
    )
    if not isinstance(data, list):
        return {}
    try:
        return {int(entry["id"]): str(entry["name"]) for entry in data}
    except (KeyError, TypeError, ValueError) as exc:
        print(f"[!] Malformed /universe/names/ payload: {exc}")
        return {}

def get_30d_average_volume(type_id: int, region_id: int = REGION_THE_FORGE) -> float:
    """Calculates average daily regional volume over the past 30 days using cached ESI history."""
    history = fetch_market_history(type_id, region_id)
    if not history:
        return 0.0
    recent = history[-30:]
    if not recent:
        return 0.0
    return round(sum(float(day.get("volume", 0)) for day in recent) / 30.0, 2)

def fetch_jita_book_depth(type_id: int, daily_vol_30d: float = 10.0) -> tuple[int, float, dict]:
    """
    Fetches live Jita 4-4 top buy and depth-weighted sell book stats for `type_id`
    so v2 can distinguish 1-2 unit tourist lowballs from genuine supply saturation.
    """
    from services.strategy_versions import compute_book_depth_stats

    url = f"{ESI_BASE_URL}/markets/{REGION_THE_FORGE}/orders/"
    orders = _esi_request_json(
        "GET",
        url,
        params={"datasource": "tranquility", "order_type": "all", "type_id": type_id},
        timeout=8,
        context=f"book_depth type_id={type_id}",
    )
    if not isinstance(orders, list):
        return type_id, 0.0, compute_book_depth_stats([], daily_vol_30d)

    try:
        jita_buys = [o["price"] for o in orders if o.get("location_id") == JITA_STATION_ID and o.get("is_buy_order")]
        jita_sells = [
            (o["price"], o.get("volume_remain", 1))
            for o in orders
            if o.get("location_id") == JITA_STATION_ID and not o.get("is_buy_order")
        ]
        top_buy = max(jita_buys) if jita_buys else 0.0
        return type_id, top_buy, compute_book_depth_stats(jita_sells, daily_vol_30d)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"[!] Malformed book depth payload for type_id={type_id}: {exc}")
        return type_id, 0.0, compute_book_depth_stats([], daily_vol_30d)