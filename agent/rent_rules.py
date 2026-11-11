"""Rent increase limits from facts.yaml (SPEC.md TOOL-6).

Code, not the model, looks up the guideline for a lease start date and the
Good Cause local rent standard, so a number in a reply always comes from the
facts sheet that sources/validate.py checks against the saved sources.
"""

from __future__ import annotations

from datetime import date

from agent.config import load_facts, load_registry


def stabilized_guideline(lease_start: date, term_years: int) -> dict:
    """Rent Guidelines Board guideline for a rent-stabilized renewal, by the NEW lease's start date."""
    if term_years not in (1, 2):
        return {"ok": False, "error": "invalid_argument",
                "reason": "term_years must be 1 or 2 (leases up to 12 months count as 1 year, up to 24 as 2)"}
    for order in load_facts()["rgb_apartment_orders"]:
        if order["lease_start_from"] <= lease_start <= order["lease_start_to"]:
            field = "one_year_lease_increase_percent" if term_years == 1 else "two_year_lease_increase_percent"
            return {
                "ok": True, "order": order["order"], "guideline_percent": order[field],
                "window": f"{order['lease_start_from']} to {order['lease_start_to']}",
                "note": ("This is the guideline increase. Orders apply together with other adjustments the law "
                         "allows, so an increase above it is not necessarily unlawful."),
                "source_url": order["source"]["url"],
            }
    return {"ok": False, "error": "no_order_for_date",
            "reason": (f"No stored Rent Guidelines Board order covers a lease starting {lease_start}. Do not "
                       f"state a guideline for it."),
            "orders_page": load_registry()["rgb-apartment-orders"]["url"]}


def good_cause_standard() -> dict:
    """The current Good Cause local rent standard and the exemptions to ask about."""
    gc = load_facts()["good_cause"]
    lrs = gc["local_rent_standard"]
    latest = lrs["published"][0]
    ex = gc["exemptions"]
    lux = ex["luxury_threshold_usd_by_unit_size"]
    return {
        "ok": True,
        "local_rent_standard_percent": latest["local_rent_standard_percent"],
        "how_computed": (f"{lrs['inflation_index_base_percent']}% plus the {latest['cpi_period']} CPI change of "
                         f"{latest['cpi_change_percent']}% for the {lrs['cpi_region']} region, capped at "
                         f"{lrs['cap_percent']}%"),
        "published_on": str(latest["published_on"]),
        "rule": ("An increase above the local rent standard in a calendar year is presumed unreasonable; the "
                 "landlord can try to rebut that in court. An increase at or below it cannot be found unreasonable."),
        "applies_if_not_exempt": True,
        "exemptions_to_check": {
            "rent_regulated_units": "rent-stabilized or rent-controlled units are covered by other rules instead",
            "small_landlord": f"landlord owns no more than {ex['small_landlord_max_units']} units in New York State",
            "owner_occupied": f"owner lives in a building of no more than {ex['owner_occupied_max_units']} units",
            "new_construction": (f"certificate of occupancy issued on or after {ex['new_construction_co_on_or_after']}"
                                 f", for {ex['new_construction_years']} years"),
            "condo_or_coop": "unit is a condominium or cooperative",
            "income_restricted": "unit must be affordable at a specific income level (for example, a lottery unit)",
            "luxury": (f"monthly rent above {ex['luxury_threshold_percent_of_fmr']}% of fair market rent, in NYC: "
                       f"studio ${lux['studio']:,}, 1BR ${lux['one_bedroom']:,}, 2BR ${lux['two_bedroom']:,}, "
                       f"3BR ${lux['three_bedroom']:,}, 4BR ${lux['four_bedroom']:,}; at or below the amount is "
                       f"not exempt"),
        },
        "source_urls": [gc["local_rent_standard"]["source"]["url"], latest["source"]["url"],
                        ex["source"]["url"]],
    }
