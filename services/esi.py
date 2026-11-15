import os
import json
import time
import base64
import urllib.parse
import requests
from datetime import datetime, timezone

EVE_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
EVE_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
ESI_BASE_URL = "https://esi.evetech.net/latest"

CLIENT_ID = os.getenv("EVE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("EVE_CLIENT_SECRET", "")
CALLBACK_URL = os.getenv("EVE_CALLBACK_URL", "http://localhost:8000/sso/callback")
SCOPES = "esi-markets.read_character_orders.v1 esi-wallet.read_character_wallet.v1 esi-assets.read_assets.v1"

SSO_TOKEN_FILE = "ledgers/sso_token.json"
ORDERS_CACHE_FILE = "ledgers/character_orders.json"
ASSETS_CACHE_FILE = "ledgers/character_assets.json"

JITA_STATION_ID = 60003760

def get_auth_url() -> str:
    params = {
        "response_type": "code",
        "redirect_uri": CALLBACK_URL,
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "state": "eve_trading_desk_secure"
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




def exchange_auth_code(code: str) -> dict:
    if not CLIENT_SECRET or CLIENT_SECRET == "YOUR_CLIENT_SECRET_HERE":
        print("[!] CRITICAL: EVE_CLIENT_SECRET is missing or set to placeholder in docker-compose.yml!")
        return {}

    headers = {
        **get_basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "login.eveonline.com"
    }
    data = {"grant_type": "authorization_code", "code": code}

    try:
        res = requests.post(EVE_TOKEN_URL, data=data, headers=headers, timeout=12)
        if res.status_code == 200:
            tokens = res.json()
            claims = decode_jwt_payload(tokens["access_token"])
            sub = claims.get("sub", "")
            char_id = int(sub.split(":")[-1]) if ":" in sub else None
            char_name = claims.get("name", "Unknown Pilot")

            bundle = {
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "character_id": char_id,
                "character_name": char_name
            }
            save_sso_tokens(bundle)
            print(f"[+] Successfully authenticated pilot: {char_name} (ID: {char_id})")
            return bundle
        else:
            print(f"[!] CCP Token Exchange Rejected: HTTP {res.status_code} - {res.text}")
    except Exception as e:
        print(f"[!] Exception during token exchange: {str(e)}")
    return {}



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
                info = {"order_id": o["order_id"], "price": o["price"], "volume_remain": o["volume_remain"], "escrow": float(o.get("escrow") or (o["price"] * o["volume_remain"]))}
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

WALLET_CACHE_FILE = "data/wallet_cache.json"

def fetch_wallet_balance(force_refresh: bool = False) -> float:
    """
    Fetches the live liquid ISK balance of the authenticated character.
    Caches balance locally for 60 seconds to avoid ESI 429 rate limits.
    Falls back to last known balance on rate limit or network failure.
    """
    import time
    tokens = load_sso_tokens()
    if not tokens or not tokens.get("character_id"):
        return 0.0

    cid = tokens["character_id"]
    now = time.time()
    cached_val = 0.0

    if os.path.exists(WALLET_CACHE_FILE):
        try:
            with open(WALLET_CACHE_FILE, "r") as f:
                cdata = json.load(f)
                if cdata.get("character_id") == cid:
                    cached_val = float(cdata.get("balance", 0.0))
                    if not force_refresh and (now - cdata.get("timestamp", 0) < 300):
                        return cached_val
        except Exception:
            pass

    token = get_valid_access_token()
    if not token:
        return cached_val

    url = f"{ESI_BASE_URL}/characters/{cid}/wallet/?datasource=tranquility"
    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if res.status_code == 200:
            bal = float(res.text)
            try:
                os.makedirs(os.path.dirname(WALLET_CACHE_FILE), exist_ok=True)
                with open(WALLET_CACHE_FILE, "w") as f:
                    json.dump({"character_id": cid, "balance": bal, "timestamp": now}, f)
            except Exception:
                pass
            return bal
        elif res.status_code == 429:
            return cached_val
        else:
            return cached_val
    except Exception as e:
        print(f"[!] Wallet balance fetch failed: {str(e)}")
        return cached_val


def fetch_wallet_transactions():
    tokens = load_sso_tokens()
    if not tokens or not tokens.get("character_id"):
        return []
    token = get_valid_access_token()
    if not token:
        return []
    url = f"{ESI_BASE_URL}/characters/{tokens['character_id']}/wallet/transactions/?datasource=tranquility"
    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[!] Wallet fetch failed: {str(e)}")
    return []

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
HISTORY_CACHE_FILE = "data/history_cache.json"
ESI_HEADERS = {"User-Agent": "eve-trading-desk-app/1.0 (local trading bot)"}

_MEM_HISTORY_CACHE = None

def _load_history_cache():
    global _MEM_HISTORY_CACHE
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

    entry = cache.get(str(type_id))
    if entry and (now - entry.get("timestamp", 0) < 43200):
        return entry.get("history", [])

    url = f"https://esi.evetech.net/latest/markets/{region_id}/history/?datasource=tranquility&type_id={type_id}"
    try:
        res = requests.get(url, headers=ESI_HEADERS, timeout=(3.05, 8.0))
        if res.status_code == 200:
            history = res.json()
            cache[str(type_id)] = {"timestamp": now, "history": history}
            with open(HISTORY_CACHE_FILE, "w") as f:
                json.dump(cache, f)
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
