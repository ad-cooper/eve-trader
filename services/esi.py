import os
import json
import time
import base64
import urllib.parse
import requests
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from services.storage import read_json, write_json

from constants import (
    ASSETS_CACHE_FILE,
    CALLBACK_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    ESI_BASE_URL,
    ESI_HEADERS,
    EVE_AUTH_URL,
    EVE_TOKEN_URL,
    HISTORY_CACHE_FILE,
    ORDERS_CACHE_FILE,
    JITA_STATION_ID,
    JITA_SYSTEM_ID,
    JUMP_ORDER_RANGES,
    PERIMETER_SYSTEM_ID,
    REGION_THE_FORGE,
    SCOPES,
    SSO_TOKEN_FILE,
    WALLET_CACHE_FILE,
    WALLET_TX_CACHE_FILE
)

def create_esi_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=retries)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "EVE-Desk-ESIClient/2.0"})
    return session


def get_auth_url(state: str = "primary") -> str:
    params = {
        "response_type": "code",
        "redirect_uri": CALLBACK_URL,
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "state": state or "primary",
    }
    return f"{EVE_AUTH_URL}?{urllib.parse.urlencode(params)}"

def get_basic_auth_header():
    creds = f"{CLIENT_ID}:{CLIENT_SECRET}"
    return {"Authorization": f"Basic {base64.b64encode(creds.encode('utf-8')).decode('utf-8')}"}

def decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))

def _read_sso_array() -> list[dict]:
    if not os.path.exists(SSO_TOKEN_FILE):
        return []
    try:
        with open(SSO_TOKEN_FILE, "r") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(raw, dict):
            nested = raw.pop("characters", None)
            arr = [raw] if raw.get("refresh_token") else []
            if isinstance(nested, dict):
                arr.extend(v for v in nested.values() if isinstance(v, dict))
            return arr
    except Exception:
        pass
    return []

def load_sso_tokens(character_name: str = None):
    arr = _read_sso_array()
    if not arr:
        return None
    if not character_name:
        return arr[0]
    target = character_name.strip().lower()
    for entry in arr:
        if str(entry.get("character_name", "")).strip().lower() == target:
            return entry
    return None

def save_sso_tokens(tokens: dict, character_name: str = None):
    os.makedirs("ledgers", exist_ok=True)
    arr = _read_sso_array()
    target_id = tokens.get("character_id")
    target_name = (character_name or tokens.get("character_name") or "").strip().lower()

    updated = False
    for idx, entry in enumerate(arr):
        same_id = target_id and entry.get("character_id") == target_id
        same_name = target_name and str(entry.get("character_name", "")).strip().lower() == target_name
        if same_id or same_name:
            arr[idx] = {**entry, **tokens}
            updated = True
            break

    if not updated:
        arr.append(dict(tokens))

    with open(SSO_TOKEN_FILE, "w") as f:
        json.dump(arr, f, indent=2)

def get_valid_access_token(character_name: str = None):
    tokens = load_sso_tokens(character_name)
    if not tokens or "refresh_token" not in tokens:
        return None

    # Check if existing access token is still valid (with 90s safety margin)
    existing_acc = tokens.get("access_token")
    if existing_acc:
        try:
            claims = decode_jwt_payload(existing_acc)
            exp = claims.get("exp", 0)
            if exp > (time.time() + 90):
                return existing_acc
        except Exception:
            pass
    headers = {
        **get_basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "login.eveonline.com"
    }
    data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    label = character_name or tokens.get("character_name") or "primary"
    try:
        res = requests.post(EVE_TOKEN_URL, data=data, headers=headers, timeout=10)
        if res.status_code == 200:
            new_data = res.json()
            tokens["access_token"] = new_data["access_token"]
            tokens["refresh_token"] = new_data.get("refresh_token", tokens["refresh_token"])
            save_sso_tokens(tokens, character_name)
            return tokens["access_token"]
        else:
            print(f"[!] Token Refresh Rejected ({label}): HTTP {res.status_code} - {res.text}")
    except Exception as e:
        print(f"[!] Token refresh failed ({label}): {str(e)}")
    return None

def get_character_auth(character_name: str = None) -> tuple[str | None, int | None]:
    """Returns (valid_access_token, character_id) for a pilot in the ledgers/sso_token.json array."""
    tokens = load_sso_tokens(character_name)
    if not tokens:
        return None, None
    access_token = get_valid_access_token(character_name)
    char_id = tokens.get("character_id") or tokens.get("CharacterID")
    if not char_id and access_token:
        claims = decode_jwt_payload(access_token)
        sub = claims.get("sub", "")
        if ":" in sub:
            char_id = int(sub.split(":")[-1])
            tokens["character_id"] = char_id
            if not tokens.get("character_name") and claims.get("name"):
                tokens["character_name"] = claims["name"]
            save_sso_tokens(tokens, character_name)
    return access_token, int(char_id) if char_id else None




def exchange_auth_code(code: str, role: str = "primary") -> dict:
    """Exchanges an EVE SSO auth code for tokens, supporting both primary (index 0) and secondary roles."""
    if not CLIENT_SECRET or CLIENT_SECRET == "YOUR_CLIENT_SECRET_HERE":
        print("[!] CRITICAL: EVE_CLIENT_SECRET is missing or set to placeholder in docker-compose.yml!")
        return {}

    headers = {
        **get_basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "login.eveonline.com",
    }
    data = {"grant_type": "authorization_code", "code": code}

    try:
        res = requests.post(EVE_TOKEN_URL, data=data, headers=headers, timeout=12)
        if res.status_code != 200:
            print(f"[!] CCP Token Exchange Rejected ({role}): HTTP {res.status_code} - {res.text}")
            return {}

        tokens = res.json()
        claims = decode_jwt_payload(tokens["access_token"])
        sub = claims.get("sub", "")
        char_id = int(sub.split(":")[-1]) if ":" in sub else None
        char_name = claims.get("name", "Unknown Pilot")

        bundle = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": tokens.get("token_type", "Bearer"),
            "expires_at": time.time() + float(tokens.get("expires_in", 1199)),
            "character_id": char_id,
            "character_name": char_name,
            "role": role or "primary",
        }

        if role and role != "primary":
            os.makedirs("ledgers", exist_ok=True)
            arr = _read_sso_array()
            primary = arr[0] if arr else None
            kept = [primary] if primary else []
            for item in arr[1:]:
                cname = str(item.get("character_name") or "")
                if role == "cj6mt" and (
                    item.get("role") == "cj6mt"
                    or cname.lower() == "skie ankora"
                    or cname.lower() == char_name.lower()
                ):
                    continue
                kept.append(item)
            if primary and str(primary.get("character_name") or "").lower() == char_name.lower():
                kept[0] = {**primary, **bundle, "role": "primary"}
            else:
                kept.append(bundle)
            with open(SSO_TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(kept, f, indent=2)
            print(f"[+] Linked {role} character: {char_name} (ID: {char_id})")
        else:
            save_sso_tokens(bundle)
            print(f"[+] Successfully authenticated primary pilot: {char_name} (ID: {char_id})")

        return bundle
    except Exception as e:
        print(f"[!] Exception during token exchange: {str(e)}")
    return {}


def resolve_primary_character() -> tuple[str | None, str | None, int | None]:
    """Resolves (character_name, valid_access_token, character_id) for the primary pilot."""
    tokens = load_sso_tokens()
    if not tokens:
        return None, None, None
    cname = tokens.get("character_name")
    tok, cid = get_character_auth(cname)
    return cname, tok, cid


def resolve_role_character(role: str = "cj6mt", default_name: str = "Skie Ankora") -> tuple[str, str, int]:
    """Resolves (character_name, valid_access_token, character_id) for a secondary role (e.g. C-J6MT seller)."""
    target_name = default_name
    arr = _read_sso_array()
    if arr:
        primary_name = str(arr[0].get("character_name") or "")
        tagged = [x for x in arr if x.get("role") == role and x.get("character_name")]
        if tagged:
            target_name = str(tagged[-1]["character_name"])
        else:
            others = [
                str(x["character_name"])
                for x in arr[1:]
                if x.get("character_name") and str(x["character_name"]) != primary_name
            ]
            if others:
                target_name = others[-1]

    token, char_id = get_character_auth(target_name)
    if not token or not char_id:
        raise RuntimeError(
            f"Unable to load or refresh '{target_name}' token from {SSO_TOKEN_FILE}. "
            f"Click 'C-J6MT Seller (Switch)' in the Imports tab or visit /sso/login?role={role}"
        )
    return target_name, token, int(char_id)



def fetch_character_orders():
    tokens = load_sso_tokens()
    if not tokens or not tokens.get("character_id"):
        return None
    token = get_valid_access_token()
    if not token:
        return None

    url = f"{ESI_BASE_URL}/characters/{tokens['character_id']}/orders/?datasource=tranquility"
    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=12)
        if res.status_code == 200:
            raw = res.json()
            buys, sells = {}, {}
            escrow = 0.0
            for o in raw:
                tid = o["type_id"]
                info = {"order_id": o["order_id"], "price": o["price"], "volume_remain": o["volume_remain"], "volume_total": o.get("volume_total", o["volume_remain"]), "escrow": float(o.get("escrow") or (o["price"] * o["volume_remain"]))}
                if o.get("is_buy_order"):
                    buys[tid] = info
                    escrow += o.get("escrow", o["price"] * o["volume_remain"])
                else:
                    sells[tid] = info
            cache = {"buys": buys, "sells": sells, "count": len(raw), "total_escrow": escrow}
            with open(ORDERS_CACHE_FILE, "w") as f:
                json.dump(cache, f, indent=2)
            return cache
    except Exception as e:
        print(f"[!] Order fetch failed: {str(e)}")
    return None

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
        url = f"{ESI_BASE_URL}/characters/{cid}/wallet/?datasource=tranquility"
        try:
            res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if res.status_code == 200:
                return float(res.text)
        except Exception as e:
            print(f"[!] Wallet balance fetch failed: {e}")
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
        url = f"{ESI_BASE_URL}/characters/{cid}/wallet/transactions/?datasource=tranquility"
        try:
            res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if res.status_code == 200:
                return res.json()
        except Exception as e:
            print(f"[!] Wallet transactions fetch failed: {e}")
        return None

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

    while True:
        url = f"{ESI_BASE_URL}/characters/{char_id}/assets/?datasource=tranquility&page={page}"
        try:
            res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=12)
            if res.status_code == 200:
                data = res.json()
                if not data:
                    break
                all_assets.extend(data)
                page += 1
            else:
                break
        except Exception:
            break

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
        try:
            import time
            mtime = os.path.getmtime(ASSETS_CACHE_FILE)
            if (time.time() - mtime) > max_age_seconds:
                return []
            with open(ASSETS_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


# ==========================================

# ==========================================
# MARKET HISTORY & VOLUME GUARD
# ==========================================


import threading

_MEM_HISTORY_CACHE = None
_HISTORY_CACHE_LOCK = threading.Lock()

def _load_history_cache():
    global _MEM_HISTORY_CACHE
    with _HISTORY_CACHE_LOCK:
        if _MEM_HISTORY_CACHE is not None:
            return _MEM_HISTORY_CACHE
        if os.path.exists(HISTORY_CACHE_FILE):
            try:
                with open(HISTORY_CACHE_FILE, "r") as f:
                    _MEM_HISTORY_CACHE = json.load(f)
                    return _MEM_HISTORY_CACHE
            except Exception:
                pass
        _MEM_HISTORY_CACHE = {}
        return _MEM_HISTORY_CACHE

def fetch_market_history(type_id: int, region_id: int = 10000002) -> list:
    """Fetches Jita daily market history from ESI, cached in-memory and on disk."""
    import time, requests
    now = time.time()
    cache = _load_history_cache()

    with _HISTORY_CACHE_LOCK:
        entry = cache.get(str(type_id))
    if entry and (now - entry.get("timestamp", 0) < 43200):
        return entry.get("history", [])

    url = f"https://esi.evetech.net/latest/markets/{region_id}/history/?datasource=tranquility&type_id={type_id}"
    try:
        res = requests.get(url, headers=ESI_HEADERS, timeout=(3.05, 8.0))
        if res.status_code == 200:
            history = res.json()
            with _HISTORY_CACHE_LOCK:
                cache[str(type_id)] = {"timestamp": now, "history": history}
                snapshot = dict(cache)
            tmp_file = f"{HISTORY_CACHE_FILE}.{threading.get_ident()}.tmp"
            with open(tmp_file, "w") as f:
                json.dump(snapshot, f)
            os.replace(tmp_file, HISTORY_CACHE_FILE)
            return history
        else:
            print(f"[!] ESI history HTTP {res.status_code} for type {type_id}")
    except Exception as e:
        print(f"[!] History fetch timeout/error for type {type_id}: {e}")

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
    while True:
        try:
            r = requests.get(
                f"{ESI_BASE_URL}/characters/{character_id}/assets/?datasource=tranquility&page={page}",
                headers=headers,
                timeout=10,
            )
            if r.status_code != 200:
                break
            rows = r.json()
            if not rows:
                break
            all_assets.extend(rows)
            total_pages = int(r.headers.get("X-Pages", 1))
            if page >= total_pages:
                break
            page += 1
        except Exception:
            break

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

    try:
        r_ord = requests.get(
            f"{ESI_BASE_URL}/characters/{character_id}/orders/?datasource=tranquility",
            headers=headers,
            timeout=10,
        )
        if r_ord.status_code == 200:
            for o in r_ord.json():
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
    except Exception:
        pass

    return result


def fetch_structure_order_book(structure_id: int, access_token: str) -> tuple[dict, list[dict]]:
    """
    Downloads all pages of a player structure's market via ESI and returns:
      (aggregated_book_by_type_id, raw_orders_list)
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    first = requests.get(
        f"{ESI_BASE_URL}/markets/structures/{structure_id}/?datasource=tranquility&page=1",
        headers=headers,
        timeout=10,
    )
    first.raise_for_status()
    total_pages = int(first.headers.get("X-Pages", 1))
    all_orders = list(first.json())

    if total_pages > 1:
        def get_page(p: int):
            r = requests.get(
                f"{ESI_BASE_URL}/markets/structures/{structure_id}/?datasource=tranquility&page={p}",
                headers=headers,
                timeout=10,
            )
            return r.json() if r.status_code == 200 else []

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


def fetch_lowest_sell_order(session: requests.Session, type_id: int, station_id: int = JITA_STATION_ID, region_id: int = REGION_THE_FORGE):
    url = f"{ESI_BASE_URL}/markets/{region_id}/orders/?datasource=tranquility&order_type=sell&type_id={type_id}"
    try:
        r = session.get(url, timeout=7)
        if r.status_code == 200:
            orders = [o for o in r.json() if o.get("location_id") == station_id]
            if orders:
                return type_id, min(o["price"] for o in orders)
    except Exception:
        pass
    return type_id, None

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


def fetch_top_buy_order(
    session: requests.Session,
    type_id: int,
    station_id: int = JITA_STATION_ID,
    region_id: int = REGION_THE_FORGE,
):
    url = f"{ESI_BASE_URL}/markets/{region_id}/orders/?datasource=tranquility&order_type=buy&type_id={type_id}"
    try:
        r = session.get(url, timeout=7)
        if r.status_code == 200:
            return type_id, extract_top_buy_for_station(r.json(), station_id=station_id)
    except Exception:
        pass
    return type_id, None

def fetch_jita_book_top(type_id: int, session: requests.Session | None = None) -> tuple[int, float, float]:
    """Fetches (type_id, top_buy, lowest_sell) in Jita 4-4 via a single ESI order_type=all query."""
    client = session or requests
    try:
        r = client.get(
            f"{ESI_BASE_URL}/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={type_id}",
            headers=ESI_HEADERS,
            timeout=8,
        )
        if r.status_code != 200:
            return type_id, 0.0, 0.0
        orders = r.json()
        top_buy = extract_top_buy_for_station(orders, station_id=JITA_STATION_ID) or 0.0
        jita_sells = [o["price"] for o in orders if o.get("location_id") == JITA_STATION_ID and not o.get("is_buy_order")]
        return type_id, top_buy, (min(jita_sells) if jita_sells else 0.0)
    except Exception:
        return type_id, 0.0, 0.0

def fetch_universe_names(ids: list[int]) -> dict[int, str]:
    """Resolves EVE universe IDs to names via POST /universe/names/."""
    clean_ids = sorted({int(i) for i in (ids or []) if i})
    if not clean_ids:
        return {}
    try:
        res = requests.post(
            f"{ESI_BASE_URL}/universe/names/?datasource=tranquility",
            json=clean_ids,
            headers=ESI_HEADERS,
            timeout=6,
        )
        if res.status_code == 200:
            return {int(entry["id"]): str(entry["name"]) for entry in res.json()}
    except Exception:
        pass
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

    url = f"{ESI_BASE_URL}/markets/{REGION_THE_FORGE}/orders/?datasource=tranquility&order_type=all&type_id={type_id}"
    try:
        res = requests.get(url, timeout=8)
        if res.status_code != 200:
            return type_id, 0.0, compute_book_depth_stats([], daily_vol_30d)
        orders = res.json()
        jita_buys = [o["price"] for o in orders if o.get("location_id") == JITA_STATION_ID and o.get("is_buy_order")]
        jita_sells = [
            (o["price"], o.get("volume_remain", 1))
            for o in orders
            if o.get("location_id") == JITA_STATION_ID and not o.get("is_buy_order")
        ]
        top_buy = max(jita_buys) if jita_buys else 0.0
        return type_id, top_buy, compute_book_depth_stats(jita_sells, daily_vol_30d)
    except Exception:
        return type_id, 0.0, compute_book_depth_stats([], daily_vol_30d)
