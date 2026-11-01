import os
import json
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

def load_sso_tokens():
    if os.path.exists(SSO_TOKEN_FILE):
        try:
            with open(SSO_TOKEN_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_sso_tokens(tokens: dict):
    os.makedirs("ledgers", exist_ok=True)
    with open(SSO_TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)

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

def get_valid_access_token():
    tokens = load_sso_tokens()
    if not tokens or "refresh_token" not in tokens:
        return None
    headers = {
        **get_basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Host": "login.eveonline.com"
    }
    data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    try:
        res = requests.post(EVE_TOKEN_URL, data=data, headers=headers, timeout=10)
        if res.status_code == 200:
            new_data = res.json()
            tokens["access_token"] = new_data["access_token"]
            tokens["refresh_token"] = new_data.get("refresh_token", tokens["refresh_token"])
            save_sso_tokens(tokens)
            return tokens["access_token"]
        else:
            print(f"[!] Token Refresh Rejected: HTTP {res.status_code} - {res.text}")
    except Exception as e:
        print(f"[!] Token refresh failed: {str(e)}")
    return None

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
                info = {"order_id": o["order_id"], "price": o["price"], "volume_remain": o["volume_remain"]}
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

def load_cached_assets():
    if os.path.exists(ASSETS_CACHE_FILE):
        try:
            with open(ASSETS_CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []
