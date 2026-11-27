"""
Join an inbox record (gpr.line_inbox) with booking fields extracted by Bo
(CHAT_PLAYBOOK section 3) and run the existing pricing engine (gpr.pricing).
No new pricing logic lives here.

    python -m gpr.handoff --record rec.json --booking booking.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from gpr.pricing import load, quote, resolve_route, summary_th


def price_booking(record: dict, booking: dict, data: dict | None = None) -> dict:
    data = data or load()
    route = booking.get("route_id") or resolve_route(booking.get("from", ""), booking.get("to", ""), data)
    bags = booking.get("bags") or {}
    sizes = bags.get("sizes_inch") or ([bags["large_size_inch"]] * bags.get("large", 0) if bags.get("large_size_inch") else None)
    q = quote(
        route,
        time=booking.get("time"),
        adults=booking.get("adults"),
        children=booking.get("children", 0),
        infants=booking.get("infants", 0),
        bag_sizes_inch=sizes,
        bag_count=(bags.get("large") or 0) + (bags.get("carry_on") or 0) or None,
        child_seat_asked=booking.get("child_seat_asked", False),
        stops=booking.get("stops", 0),
        discount_requested=booking.get("discount_requested", False),
        data=data,
    )
    return {
        "dedup_key": record.get("dedup_key"),
        "env": record.get("env"),
        "customer_ref": (record.get("source") or {}).get("customer_ref"),
        "route_id": route,
        "quote": q.to_dict(),
        "summary_th": summary_th(q),
        "group": "ส่งให้ Gift" if q.escalate else "ทั่วไป",
        "reply_allowed": False,
        "booking_confirmed": False,
        "payment_confirmed": False,
    }


def main() -> None:
    ap = argparse.ArgumentParser(prog="gpr.handoff")
    ap.add_argument("--record", type=Path, required=True)
    ap.add_argument("--booking", type=Path, required=True)
    a = ap.parse_args()
    out = price_booking(json.loads(a.record.read_text(encoding="utf-8")), json.loads(a.booking.read_text(encoding="utf-8")))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
