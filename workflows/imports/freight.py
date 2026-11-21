import math


def calc_hisec_single_contract(m3: float, collateral: float, warps: int = 35, lowsec_warps: int = 0) -> tuple[str, float]:
    """
    Exact single-contract reward for highsec haulage:
      - Small (0 - 62,500 m3, up to 10B): 1.5M/warp * mult (1.0x <=1.5B, 2.0x..5.0x by 0.5B step up to 5B, +20M/B to 10B)
      - Freighter (<= 950,000 m3, up to 5B): 2.25M/warp (1-30w) or 2.5M/warp (31+w) * mult (1.0x <=1.5B, 2.0x <=3B, 4.0x <=5B)
      - JF-Large (<= 360,000 m3, up to 50B): 200M base + 100M/lowsec + collateral tier (+50M <=10B, +150M <=15B, +100M per 5B)
    """
    if m3 <= 62_500.0 and collateral <= 10_000_000_000.0:
        base = warps * 1_500_000.0
        if collateral <= 1_500_000_000.0:
            mult, extra = 1.0, 0.0
        elif collateral <= 5_000_000_000.0:
            mult = math.ceil((collateral / 1e9) * 2.0) / 2.0
            extra = 0.0
        else:
            mult = 5.0
            extra = math.ceil((collateral - 5_000_000_000.0) / 1e9) * 20_000_000.0
        return "Small (DST)", base * mult + extra

    if m3 <= 950_000.0 and collateral <= 5_000_000_000.0:
        per_warp = 2_250_000.0 if warps <= 30 else 2_500_000.0
        base = warps * per_warp
        if collateral <= 1_500_000_000.0:
            mult = 1.0
        elif collateral <= 3_000_000_000.0:
            mult = 2.0
        else:
            mult = 4.0
        return "Freighter", base * mult

    if m3 <= 360_000.0 and collateral <= 50_000_000_000.0:
        base = 200_000_000.0 + (lowsec_warps * 100_000_000.0)
        if collateral <= 5_000_000_000.0:
            extra = 0.0
        elif collateral <= 10_000_000_000.0:
            extra = 50_000_000.0
        else:
            extra = 50_000_000.0 + math.ceil((collateral - 10_000_000_000.0) / 5_000_000_000.0) * 100_000_000.0
        return "JF-Large", base + extra

    return "Oversized", float("inf")


def calc_hisec_optimal_split(total_m3: float, total_collateral: float, warps: int = 35, lowsec_warps: int = 0) -> dict:
    """
    Finds the minimum-cost contract split across Small (DST) and Freighter tiers,
    keeping each split at or below the 1.5B Tier-1 collateral ceiling whenever cheaper.
    """
    if total_m3 <= 0 and total_collateral <= 0:
        return {"contracts": 0, "plan": "0 contracts", "total_reward": 0.0}

    best = None

    # 1. Homogeneous splits (1..N identical contracts)
    min_n = max(1, math.ceil(total_m3 / 950_000.0), math.ceil(total_collateral / 50_000_000_000.0))
    max_n = max(min_n, math.ceil(total_m3 / 62_500.0), math.ceil(total_collateral / 1_500_000_000.0)) + 1
    for n in range(min_n, max_n + 1):
        tier, reward = calc_hisec_single_contract(total_m3 / n, total_collateral / n, warps, lowsec_warps)
        if not math.isinf(reward):
            tot = n * reward
            if best is None or tot < best["total_reward"]:
                best = {
                    "contracts": n,
                    "plan": f"{n}x {tier} @ {reward / 1e6:.1f}M",
                    "total_reward": round(tot, 2),
                }

    # 2. Mixed Tier-1 (<= 1.5B collateral) Freighter + Small (DST) splits
    f_cost = warps * (2_250_000.0 if warps <= 30 else 2_500_000.0)
    s_cost = warps * 1_500_000.0
    max_f = math.ceil(total_m3 / 950_000.0) + 1
    for f in range(0, max_f + 1):
        rem_m3 = max(0.0, total_m3 - f * 950_000.0)
        rem_col = max(0.0, total_collateral - f * 1_500_000_000.0)
        s = max(math.ceil(rem_m3 / 62_500.0), math.ceil(rem_col / 1_500_000_000.0))
        if f + s == 0:
            continue
        tot = f * f_cost + s * s_cost
        if best is None or tot < best["total_reward"]:
            parts = []
            if f > 0:
                parts.append(f"{f}x Freighter @ {f_cost / 1e6:.1f}M")
            if s > 0:
                parts.append(f"{s}x Small (DST) @ {s_cost / 1e6:.1f}M")
            best = {
                "contracts": f + s,
                "plan": " + ".join(parts),
                "total_reward": round(tot, 2),
            }

    return best or {"contracts": 1, "plan": "Unroutable", "total_reward": 0.0}


def calc_courier_freight(
    total_m3: float,
    total_collateral: float,
    dest: dict,
    rate_override: float | None = None,
    apply_min: bool = True,
) -> float:
    """
    Computes courier contract reward for either:
      - 'hisec_tiered': 35-warp Small/Freighter tiered rate card with optimal <=1.5B contract splitting
      - 'per_m3': ITL-style per-m3 + collateral rate with min floor & full-load cap
    """
    if dest.get("pricing_model") == "hisec_tiered":
        warps = int(rate_override) if rate_override is not None else int(dest.get("warps", 35))
        lowsec_warps = int(dest.get("lowsec_warps", 0))
        if not apply_min:
            s_cost = warps * 1_500_000.0
            f_cost = warps * (2_250_000.0 if warps <= 30 else 2_500_000.0)
            small_marginal = max(total_m3 * (s_cost / 62_500.0), total_collateral * (s_cost / 1_500_000_000.0))
            freighter_marginal = max(total_m3 * (f_cost / 950_000.0), total_collateral * (f_cost / 1_500_000_000.0))
            return min(small_marginal, freighter_marginal)
        return calc_hisec_optimal_split(total_m3, total_collateral, warps, lowsec_warps)["total_reward"]

    rate = rate_override if rate_override is not None else dest["freight_rate_m3"]
    col_pct = dest.get("collateral_pct", 0.0)
    min_reward = dest.get("min_freight", 0.0) if apply_min else 0.0
    max_reward = dest.get("max_freight")

    col_reward = total_collateral * col_pct
    vol_reward = total_m3 * rate
    if min_reward > 0 and vol_reward < min_reward:
        vol_reward = min_reward
    if max_reward is not None and max_reward > 0 and vol_reward > max_reward:
        vol_reward = max_reward

    return max(col_reward, vol_reward)
