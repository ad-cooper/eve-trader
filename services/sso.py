import base64
import json
import os
import requests
import time
import urllib

from constants import (
    CALLBACK_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    EVE_AUTH_URL,
    EVE_TOKEN_URL,
    SCOPES,
    SSO_TOKEN_FILE,
)


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

def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))

def get_valid_access_token(character_name: str = None):
    tokens = load_sso_tokens(character_name)
    if not tokens or "refresh_token" not in tokens:
        return None

    # Check if existing access token is still valid (with 90s safety margin)
    existing_acc = tokens.get("access_token")
    if existing_acc:
        claims = _decode_jwt_payload(existing_acc)
        exp = claims.get("exp", 0)
        if exp > (time.time() + 90):
            return existing_acc
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
        claims = _decode_jwt_payload(tokens["access_token"])
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

def _get_character_auth(character_name: str = None) -> tuple[str | None, int | None]:
    """Returns (valid_access_token, character_id) for a pilot in the ledgers/sso_token.json array."""
    tokens = load_sso_tokens(character_name)
    if not tokens:
        return None, None
    access_token = get_valid_access_token(character_name)
    char_id = tokens.get("character_id") or tokens.get("CharacterID")
    if not char_id and access_token:
        claims = _decode_jwt_payload(access_token)
        sub = claims.get("sub", "")
        if ":" in sub:
            char_id = int(sub.split(":")[-1])
            tokens["character_id"] = char_id
            if not tokens.get("character_name") and claims.get("name"):
                tokens["character_name"] = claims["name"]
            save_sso_tokens(tokens, character_name)
    return access_token, int(char_id) if char_id else None

def _read_sso_array() -> list[dict]:
    if not os.path.exists(SSO_TOKEN_FILE):
        return []
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

def resolve_primary_character() -> tuple[str | None, str | None, int | None]:
    """Resolves (character_name, valid_access_token, character_id) for the primary pilot."""
    tokens = load_sso_tokens()
    if not tokens:
        return None, None, None
    cname = tokens.get("character_name")
    tok, cid = _get_character_auth(cname)
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

    token, char_id = _get_character_auth(target_name)
    if not token or not char_id:
        raise RuntimeError(
            f"Unable to load or refresh '{target_name}' token from {SSO_TOKEN_FILE}. "
            f"Click 'C-J6MT Seller (Switch)' in the Imports tab or visit /sso/login?role={role}"
        )
    return target_name, token, int(char_id)