"""Acceptance criteria from Issue #1, as tests."""
from gpr.pricing import is_night, load, quote, resolve_route

D = load()


def test_prices_match_table_exactly():
    assert quote("BKK-BANGKOK", time="14:00", adults=2, bag_sizes_inch=[24], data=D).total_min == 700
    assert quote("DMK-BANGKOK", time="14:00", adults=2, bag_sizes_inch=[24], data=D).total_min == 850
    q = quote("BANGKOK-PATTAYA", time="10:00", adults=2, bag_sizes_inch=[24], data=D)
    assert (q.total_min, q.total_max) == (1500, 1700)
    assert quote("BANGKOK-KHAOYAI", time="10:00", adults=1, bag_sizes_inch=[20], data=D).total_min == 4000
    assert quote("AYUTTHAYA-DAY", time="08:00", adults=2, bag_sizes_inch=[20], data=D).total_min == 3500
    assert quote("DMK-BANGKOK", data=D).currency == "THB"


def test_night_surcharge_window():
    assert is_night("22:00", D) and is_night("23:59", D) and is_night("00:30", D) and is_night("05:59", D)
    assert not is_night("06:00", D) and not is_night("21:59", D)
    assert quote("BKK-BANGKOK", time="23:30", adults=2, bag_sizes_inch=[24], data=D).total_min == 900
    # day hire is not a per-trip route -> no night surcharge
    assert quote("AYUTTHAYA-DAY", time="05:30", adults=2, bag_sizes_inch=[24], data=D).night_surcharge == 0


def test_deposit_is_500():
    assert quote("DMK-BANGKOK", time="10:00", adults=1, bag_sizes_inch=[20], data=D).deposit == 500


def test_vehicle_limits_escalate():
    q = quote("DMK-BANGKOK", time="10:00", adults=6, infants=2, bag_sizes_inch=[24, 24], data=D)
    assert q.escalate and any("เกินที่แนะนำ" in r for r in q.escalate_reasons)
    q = quote("DMK-BANGKOK", time="10:00", adults=2, bag_sizes_inch=[30], data=D)
    assert q.escalate and any("28" in r for r in q.escalate_reasons)
    assert not quote("DMK-BANGKOK", time="10:00", adults=3, bag_sizes_inch=[28, 24], data=D).escalate


def test_missing_info_is_asked_not_guessed():
    q = quote("DMK-BANGKOK", adults=None, data=D)
    assert any("เวลา" in m for m in q.missing) and any("ผู้โดยสาร" in m for m in q.missing)
    q = quote("DMK-BANGKOK", time="10:00", adults=2, infants=1, bag_count=2, data=D)
    assert any("คาร์ซีท" in m for m in q.missing) and any("ขนาด" in m for m in q.missing)


def test_off_table_discount_stops_escalate():
    assert quote(None, time="10:00", adults=2, data=D).escalate
    assert quote(None, data=D).total_min is None  # never invent a price
    assert quote("DMK-BANGKOK", time="10:00", adults=2, bag_sizes_inch=[24], discount_requested=True, data=D).escalate
    assert quote("DMK-BANGKOK", time="10:00", adults=2, bag_sizes_inch=[24], stops=1, data=D).escalate
    assert quote("SAFARI-WORLD", time="09:00", adults=2, bag_sizes_inch=[20], data=D).escalate


def test_resolve_route_from_free_text():
    assert resolve_route("廊曼机场", "曼谷 Phetchaburi Soi 31", D) == "DMK-BANGKOK"
    assert resolve_route("Sukhumvit hotel", "BKK airport", D) == "BKK-BANGKOK"
    assert resolve_route("曼谷", "芭提雅", D) == "BANGKOK-PATTAYA"
    assert resolve_route("BKK", "Pattaya", D) is None  # two known places -> not in table
    assert resolve_route("Chiang Mai", "Bangkok", D) is None
