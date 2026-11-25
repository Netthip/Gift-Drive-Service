"""
GPR deterministic quote checker. Numbers come ONLY from knowledge/pricing.json (Issue #1).
AI agents extract the booking facts; this module decides price, surcharges and whether
the case must go to Gift. It never invents a price.

CLI:
    python -m gpr.pricing --route DMK-BANGKOK --time 23:30 --adults 2 --bags 28,24
    python -m gpr.pricing --from "廊曼机场" --to "曼谷 Phetchaburi Soi 31" --adults 6 --infants 2
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "knowledge" / "pricing.json"


def load(path: Path = DATA) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class Quote:
    route_id: str | None
    currency: str = "THB"
    base_min: int | None = None
    base_max: int | None = None
    night_surcharge: int = 0
    total_min: int | None = None
    total_max: int | None = None
    deposit: int | None = None
    checks: list[str] = field(default_factory=list)       # passed checks / notes
    missing: list[str] = field(default_factory=list)      # info to ask the customer
    escalate: bool = False
    escalate_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _minutes(hhmm: str) -> int:
    h, m = hhmm.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time: {hhmm}")
    return h * 60 + m


def is_night(hhmm: str, data: dict) -> bool:
    """22:00 <= t < 06:00 (06:00 itself is daytime)."""
    t = _minutes(hhmm)
    start, end = _minutes(data["night_surcharge"]["start"]), _minutes(data["night_surcharge"]["end"])
    return t >= start or t < end


def _place(text: str, data: dict) -> str | None:
    low = text.lower()
    for key, words in data["aliases"].items():
        if any(w.lower() in low for w in words):
            return key
    return None


def resolve_route(origin: str, destination: str, data: dict) -> str | None:
    """Map free text to a route id. Unknown non-Bangkok places -> None (escalate)."""
    a, b = _place(origin, data), _place(destination, data)
    places = {p for p in (a, b) if p}
    if places == {"BKK"}:
        return "BKK-BANGKOK"
    if places == {"DMK"}:
        return "DMK-BANGKOK"
    if places == {"PATTAYA"}:
        return "BANGKOK-PATTAYA"
    if places == {"KHAOYAI"}:
        return "BANGKOK-KHAOYAI"
    if places == {"NAKHONPATHOM"}:
        return "NAKHONPATHOM-DAY"
    if places == {"AYUTTHAYA"}:
        return "AYUTTHAYA-DAY"
    if places == {"SAFARI-WORLD"}:
        return "SAFARI-WORLD"
    return None  # e.g. BKK->Pattaya, two known places, or unknown place


def quote(
    route_id: str | None,
    *,
    time: str | None = None,
    adults: int | None = None,
    children: int = 0,
    infants: int = 0,
    bag_sizes_inch: list[int] | None = None,
    bag_count: int | None = None,
    child_seat_asked: bool = False,
    stops: int = 0,
    discount_requested: bool = False,
    data: dict | None = None,
) -> Quote:
    data = data or load()
    q = Quote(route_id=route_id, currency=data["currency"])
    veh = data["vehicle"]

    def esc(reason: str) -> None:
        q.escalate = True
        q.escalate_reasons.append(reason)

    # --- route / base price
    route = next((r for r in data["routes"] if r["id"] == route_id), None)
    if route is None:
        esc("เส้นทางไม่อยู่ในตารางราคา — ห้ามเดาราคา")
    else:
        q.base_min = route.get("price", route.get("price_min"))
        q.base_max = route.get("price", route.get("price_max"))
        if q.base_min != q.base_max:
            q.checks.append(f"ราคาเป็นช่วง {q.base_min:,}–{q.base_max:,} — Gift เลือกราคาสุดท้าย")
        if route.get("needs_detail_check"):
            esc("เส้นทางนี้ต้องตกลงรายละเอียดก่อน (Safari World)")
        if route.get("includes"):
            q.checks.append("รวม " + " และ ".join(route["includes"]))

    # --- time / night surcharge (per-trip routes only)
    if time is None:
        q.missing.append("เวลารับ (หรือเลขเที่ยวบิน + เวลาเครื่องลง)")
    elif route is not None and route["unit"] == "trip" and is_night(time, data):
        q.night_surcharge = data["night_surcharge"]["amount"]
        q.checks.append(f"เวลา {time} อยู่ช่วงกลางคืน +{q.night_surcharge}")

    # --- passengers vs MG4
    if adults is None:
        q.missing.append("จำนวนผู้โดยสาร")
    else:
        pax = adults + children + (infants if veh["infants_count_as_passengers"] else 0)
        if pax > veh["recommended_max_passengers"]:
            esc(f"ผู้โดยสาร {pax} คน เกินที่แนะนำ {veh['recommended_max_passengers']} คนสำหรับ {veh['model']} — ต้องเพิ่มรถ/ตัดสินใจโดย Gift")
        else:
            q.checks.append(f"ผู้โดยสาร {pax} คน ≤ {veh['recommended_max_passengers']} ✓")
        if (children or infants) and not child_seat_asked:
            q.missing.append("ต้องการคาร์ซีทเด็กหรือไม่")

    # --- luggage
    if bag_sizes_inch:
        too_big = [s for s in bag_sizes_inch if s > veh["max_bag_inch"]]
        if too_big:
            esc(f"กระเป๋าเกิน {veh['max_bag_inch']} นิ้ว: {too_big}")
        else:
            q.checks.append(f"กระเป๋าทุกใบ ≤ {veh['max_bag_inch']} นิ้ว ✓")
        if veh["bag_count_limit"] is None and len(bag_sizes_inch) > 2:
            q.checks.append(f"กระเป๋า {len(bag_sizes_inch)} ใบ — ยังไม่มีตัวเลขจำนวนใบสูงสุดของ {veh['model']} ให้ Gift ยืนยันว่าใส่ได้")
    elif bag_count:
        q.missing.append("ขนาดกระเป๋าใหญ่ (นิ้ว)")
    else:
        q.missing.append("จำนวนและขนาดกระเป๋า")

    # --- other escalations
    if stops:
        esc(f"มีจุดแวะ {stops} จุด — ราคานอกตาราง")
    if discount_requested:
        esc("ลูกค้าขอส่วนลด")

    # --- totals
    if q.base_min is not None:
        q.total_min = q.base_min + q.night_surcharge
        q.total_max = q.base_max + q.night_surcharge
        q.deposit = data["deposit"]["amount"]
    return q


def summary_th(q: Quote) -> str:
    lines = []
    if q.total_min is not None:
        price = f"{q.total_min:,}" if q.total_min == q.total_max else f"{q.total_min:,}–{q.total_max:,}"
        lines.append(f"ราคา: {price} {q.currency} (มัดจำ {q.deposit} หักจากยอด — ขอมัดจำได้หลังตรวจคิวจริงเท่านั้น)")
    lines += [f"✓ {c}" for c in q.checks]
    lines += [f"? ถามลูกค้า: {m}" for m in q.missing]
    if q.escalate:
        lines.append("⚠️ ส่งให้ Gift: " + "; ".join(q.escalate_reasons))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="GPR quote checker")
    ap.add_argument("--route")
    ap.add_argument("--from", dest="origin")
    ap.add_argument("--to", dest="destination")
    ap.add_argument("--time")
    ap.add_argument("--adults", type=int)
    ap.add_argument("--children", type=int, default=0)
    ap.add_argument("--infants", type=int, default=0)
    ap.add_argument("--bags", help="comma-separated sizes in inches, e.g. 28,24")
    ap.add_argument("--bag-count", type=int)
    ap.add_argument("--stops", type=int, default=0)
    ap.add_argument("--discount", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    data = load()
    route = a.route or (resolve_route(a.origin or "", a.destination or "", data) if (a.origin or a.destination) else None)
    q = quote(
        route, time=a.time, adults=a.adults, children=a.children, infants=a.infants,
        bag_sizes_inch=[int(x) for x in a.bags.split(",")] if a.bags else None,
        bag_count=a.bag_count, stops=a.stops, discount_requested=a.discount, data=data,
    )
    print(json.dumps(q.to_dict(), ensure_ascii=False, indent=2) if a.json else f"เส้นทาง: {route}\n{summary_th(q)}")


if __name__ == "__main__":
    main()
