from workflows.imports.scanner import IMPORT_DESTINATIONS, scan_import_opportunities


def main() -> None:
    data = scan_import_opportunities()
    for dest in IMPORT_DESTINATIONS:
        sec = data[dest["key"]]
        extra = ""
        if sec.get("split_plan_top20"):
            extra = f" | Split: {sec['split_plan_top20']}"
        elif dest["type"] == "structure":
            extra = (
                f" | Tracked: {sec['tracking_days']:.2f}d "
                f"({sec['tracking_snapshots']} snaps, {sec['tracking_confidence_pct']:.0f}% conf)"
            )
        print(
            f"\n=== {dest['name']} IMPORTS ({sec['count']} items | "
            f"Top-20 Batch: {sec['multibuy_m3']:,.0f} m3, Freight: {sec['multibuy_freight']/1e6:.2f}M, "
            f"Net Profit: {sec['multibuy_profit']/1e6:.1f}M{extra}) ==="
        )
        for r in sec["items"][:12]:
            print(
                f"  {r['name']:<36} | Qty: {r['suggested_qty']:>4} (~{r['dest_daily_vol']:>4.1f}/d [{r['vol_source']}]) | "
                f"Jita: {r['jita_ask']/1e6:6.1f}M -> {r['suggested_sell']/1e6:6.1f}M | "
                f"Margin: {r['margin_pct']:5.1f}% | Est/Day: {r['est_daily_profit']/1e6:6.1f}M | {r['status']}"
            )


if __name__ == "__main__":
    main()
