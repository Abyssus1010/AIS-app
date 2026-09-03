"""Generic vessel-height lookup: AIS ship type + length overall (LOA) -> a
conservative, table-driven height estimate, in place of a per-vessel web
search. Both inputs are already broadcast for free in AIS's ShipStaticData
message (the `Type` field, and LOA = Dimension.A + Dimension.B) - see
ais_client.py's docstring on ShipStaticData - so this needs no network call,
no rate limit, and returns instantly for any vessel with static data.

The number returned is a rough, deliberately conservative (upper-bound)
estimate of the vessel's height above the waterline (i.e. directly
comparable to the app's flag threshold - see db.get_height_threshold_ft
for the live, user-adjustable value), NOT a certified figure - see the
docstring on HeightEstimate.confidence for why several buckets are much less
reliable than others, and REVIEW.md-style: these numbers are compiled from
general public naval-architecture reference points (canal/bridge air-draft
limits, published dimensions of specific well-documented vessels e.g. the
Dali, Q-Max LNG carriers, Icon-class cruise ships), not a class-society
dataset, and should be sanity-checked against real fleet data before being
trusted to clear a vessel as OK unattended.
"""

from dataclasses import dataclass
from enum import Enum


class Category(str, Enum):
    CARGO = "cargo"
    TANKER = "tanker"
    PASSENGER = "passenger"
    FISHING = "fishing"
    SMALL_WORKBOAT = "small_workboat"  # tug, pilot, SAR, port tender, law enforcement, anti-pollution
    HIGH_SPEED_CRAFT = "high_speed_craft"
    MAST_DRIVEN = "mast_driven"  # sailing vessels, pleasure craft - height set by mast, not hull size
    UNSUPPORTED = "unsupported"  # WIG, military, diving/dredging, spare/other - too rare/exotic to estimate


# AIS ship-type codes (ITU-R M.1371 Table 50, as broadcast in ShipStaticData.Type).
# Ranges are coarser than they look: 70-79 "Cargo" covers general cargo, bulk
# carriers, container ships, ro-ro AND car carriers with no further
# distinction, and 80-89 "Tanker" covers oil/chemical/product tankers AND
# LNG/LPG carriers the same way - AIS itself cannot tell these apart. See
# CARGO/TANKER confidence notes below for how that's handled.
_TYPE_CODE_TO_CATEGORY: dict[range, Category] = {
    range(30, 31): Category.FISHING,
    range(31, 35): Category.SMALL_WORKBOAT,  # towing, towing (large), dredging/underwater ops, diving ops
    # 35 (military ops) deliberately falls through to UNSUPPORTED below.
    range(36, 37): Category.MAST_DRIVEN,  # sailing
    range(37, 38): Category.MAST_DRIVEN,  # pleasure craft
    range(40, 50): Category.HIGH_SPEED_CRAFT,
    range(50, 56): Category.SMALL_WORKBOAT,  # pilot, SAR, tug, port tender, anti-pollution, law enforcement
    range(60, 70): Category.PASSENGER,
    range(70, 80): Category.CARGO,
    range(80, 90): Category.TANKER,
}


def category_for_ais_type(ais_type: int | None) -> Category:
    if ais_type is None:
        return Category.UNSUPPORTED
    for code_range, category in _TYPE_CODE_TO_CATEGORY.items():
        if ais_type in code_range:
            return category
    return Category.UNSUPPORTED  # WIG (20-29), military (35), spare/other (0, 56-59, 90-99)


# Per-category LOA brackets, in meters, as (upper_bound_exclusive, label).
# Boundaries roughly track real size-class breakpoints (Handysize/Panamax/
# Capesize for bulk+tanker, feeder/Panamax/Neopanamax/ULCV for container
# ships) rather than being evenly spaced, since that's where the real height
# jumps happen.
_CARGO_TANKER_BRACKETS = [
    (100.0, "small"),
    (200.0, "medium"),
    (300.0, "large"),
    (float("inf"), "very_large"),
]
_PASSENGER_BRACKETS = [
    (50.0, "small"),
    (150.0, "medium"),
    (250.0, "large"),
    (float("inf"), "very_large"),
]


def _bracket_for_loa(loa_m: float, brackets: list[tuple[float, str]]) -> str:
    for upper, label in brackets:
        if loa_m < upper:
            return label
    return brackets[-1][1]


@dataclass
class HeightEstimate:
    height_ft: float | None
    category: Category
    bracket: str | None
    # "medium": the category's real height range is narrow enough relative
    #   to LOA that a table lookup is a reasonable proxy (passenger, fishing,
    #   small workboats, high-speed craft).
    # "low": CARGO and TANKER only - AIS's own type code can't distinguish
    #   subtypes with very different height profiles at the same LOA (a
    #   200m car carrier or container ship vs. a 200m bulk carrier; a 300m
    #   LNG carrier vs. a 300m VLCC). The table value is deliberately set to
    #   the taller subtype's typical figure so it stays conservative, but
    #   that means it will over-flag the shorter, more common subtypes
    #   (plain bulk/general cargo, oil/product tankers) at the same size -
    #   treat a "low" confidence OK verdict as provisional, not final.
    # None: MAST_DRIVEN (height set by mast/rigging, uncorrelated with hull
    #   LOA - a 12m sailboat can carry a 20m mast) or UNSUPPORTED (too rare/
    #   exotic a type to have a reference figure at all) - no number is
    #   returned; caller should fall back to another method (e.g. an actual
    #   per-vessel lookup) rather than trust a guess here.
    confidence: str | None
    note: str


# Values are a conservative (upper-end-of-observed-range) height above the
# waterline, in feet, directly comparable to the app's flag threshold (see
# db.get_height_threshold_ft for the live, user-adjustable value). Sources/
# reasoning per bracket:
#
# CARGO - biased to the tallest common subtype at that size, since AIS can't
# tell a container ship or car carrier from a bulk carrier at the same LOA:
#   small (<100m):    general cargo/small container feeders                -> 25m / 82ft
#   medium (100-200m): Handysize bulkers up to mid-size PCTC car carriers,
#                      which get disproportionately tall (multi-deck boxy
#                      superstructure) even at this length - NEEDS
#                      VERIFICATION: one secondary-source data point for a
#                      ~200m PCTC (Pleiades Leader: 52.6m keel-to-antenna,
#                      ~8-9m submerged loaded => ~44m/144ft air draft) runs
#                      higher than this bracket's figure; not confirmed
#                      against a primary spec sheet, so left as-is pending
#                      that check rather than revised on one weak source     -> 40m / 131ft
#   large (200-300m): Panamax/Post-Panamax container ships and bulkers      -> 55m / 180ft
#   very_large (300m+): Neopanamax/ULCV container ships (e.g. Ever Ace
#                      class, ~400m) - real air drafts here run up to
#                      60-70m; the Panama Canal's own bridge-clearance
#                      ceiling for vessels that must transit it is 57.9m/
#                      190ft (en.wikipedia.org/wiki/Panamax), but larger
#                      ULCVs that never transit Panama can exceed that      -> 65m / 213ft
#
# TANKER - oil/chemical/product tankers run measurably lower than cargo
# ships of the same LOA (cargo is liquid, held below deck - the only real
# height driver is the aft accommodation block/funnel), EXCEPT LNG/LPG
# carriers sharing this same AIS code range, which the table biases toward:
#   small (<100m):    coastal chemical/product tankers, RIDING IN BALLAST -
#                      a loaded coastal tanker sits closer to ~20m/66ft, but
#                      that's within a few feet of the app's flag threshold
#                      (70ft by default - see db.get_height_threshold_ft for
#                      the live, user-adjustable value), and a tanker in
#                      ballast rides meaningfully higher out of the water
#                      than loaded (see very_large below, same effect) -
#                      bumped up to keep a
#                      margin above the flag threshold instead of sitting
#                      just under it on an unverified loaded-only figure -> 24m / 79ft
#   medium (100-200m): MR product/chemical tankers                         -> 28m / 92ft
#   large (200-300m): Aframax/Suezmax crude tankers, most LNG carriers      -> 38m / 125ft
#   very_large (300m+): VLCC/ULCC, Q-Flex/Q-Max LNG carriers (345m Q-Max
#                      structural height ~34.7m keel-to-deck per
#                      en.wikipedia.org/wiki/Q-Max, plus accommodation/mast
#                      above deck; ballast condition pushes air draft well
#                      above the loaded figure) - NEEDS VERIFICATION: the
#                      accommodation/mast addition on top of the cited 34.7m
#                      is still this table's own estimate, not a source      -> 48m / 157ft
#
# PASSENGER - doesn't scale smoothly with LOA the way cargo/tankers do,
# since even mid-size cruise ships carry many tall guest decks:
#   small (<50m):     harbor ferries, water taxis                          -> 10m / 33ft
#   medium (50-150m): coastal ferries, small expedition cruise ships        -> 25m / 82ft
#   large (150-250m): mid-size cruise ships, RoPax ferries                  -> 50m / 164ft
#   very_large (250m+): large cruise ships - confirmed via Wikipedia:
#                      Oasis-class = 72m/236ft above waterline
#                      (en.wikipedia.org/wiki/Oasis-class_cruise_ship),
#                      Icon-class = 75.6m/248ft overall height
#                      (en.wikipedia.org/wiki/Icon-class_cruise_ship) - both
#                      exceed this table's old 230ft figure, so raised to
#                      stay above the tallest confirmed class              -> 76m / 250ft
#
# FISHING / SMALL_WORKBOAT / HIGH_SPEED_CRAFT - single bracket each: height
# is driven by wheelhouse/mast/antenna on a hull that's rarely large enough
# for LOA to matter, and real-world figures cluster tightly regardless of
# the (typically small) size. NEEDS VERIFICATION: no authoritative source
# was found for any of the three during review - these remain the author's
# domain-knowledge estimates, unconfirmed against real vessel data:
#   fishing:            factory trawlers rarely exceed this even when large -> 20m / 66ft
#   small_workboat:     tug/pilot/SAR/port-tender wheelhouse height          -> 18m / 59ft
#   high_speed_craft:   low-profile catamaran/monohull ferries               -> 15m / 49ft

_HEIGHT_TABLE_FT: dict[tuple[Category, str], float] = {
    (Category.CARGO, "small"): 82.0,
    (Category.CARGO, "medium"): 131.0,
    (Category.CARGO, "large"): 180.0,
    (Category.CARGO, "very_large"): 213.0,
    (Category.TANKER, "small"): 79.0,
    (Category.TANKER, "medium"): 92.0,
    (Category.TANKER, "large"): 125.0,
    (Category.TANKER, "very_large"): 157.0,
    (Category.PASSENGER, "small"): 33.0,
    (Category.PASSENGER, "medium"): 82.0,
    (Category.PASSENGER, "large"): 164.0,
    (Category.PASSENGER, "very_large"): 250.0,
    (Category.FISHING, "any"): 66.0,
    (Category.SMALL_WORKBOAT, "any"): 59.0,
    (Category.HIGH_SPEED_CRAFT, "any"): 49.0,
}

# Human-readable "how was this number derived" text for each cell above, for
# display in the UI's full type+size table (see full_table() below). This is
# the same reasoning as the big comment block above, just written for an end
# user rather than a code reader - keep the two in sync when either changes.
# Most of these are the author's naval-architecture domain knowledge tied to
# a named reference vessel class, NOT an independently citable source; the
# two Wikipedia links are the only hard citations in the table.
_CARGO_TANKER_CAVEAT = (
    " AIS's type code can't tell this apart from shorter subtypes at the same "
    "size, so the value is biased toward the tallest common one - treat an OK "
    "verdict here as provisional."
)
_HEIGHT_TABLE_BASIS: dict[tuple[Category, str], str] = {
    (Category.CARGO, "small"): "General cargo and small container feeders." + _CARGO_TANKER_CAVEAT,
    (Category.CARGO, "medium"): (
        "Handysize bulkers up to mid-size PCTC car carriers, which run "
        "disproportionately tall (multi-deck boxy superstructure) even at this length. "
        "Needs verification: one secondary-source figure for a ~200 m PCTC (Pleiades "
        "Leader) implies an air draft nearer 144 ft than this bracket's value - not yet "
        "confirmed against a primary spec sheet."
        + _CARGO_TANKER_CAVEAT
    ),
    (Category.CARGO, "large"): "Panamax/Post-Panamax container ships and bulkers." + _CARGO_TANKER_CAVEAT,
    (Category.CARGO, "very_large"): (
        "Neopanamax/ULCV container ships (e.g. Ever Ace class, ~400 m). The Panama "
        "Canal's own bridge-clearance ceiling for Panamax transit is 57.9 m / 190 ft "
        "(en.wikipedia.org/wiki/Panamax), though larger ULCVs that skip Panama can "
        "run higher." + _CARGO_TANKER_CAVEAT
    ),
    (Category.TANKER, "small"): (
        "Coastal chemical/product tankers. A loaded vessel sits closer to 66 ft, but "
        "that's only 4 ft under this app's 70 ft flag threshold, and a tanker riding in "
        "ballast sits meaningfully higher out of the water than loaded - raised to keep "
        "a margin above the threshold rather than rely on an unverified loaded-only figure."
        + _CARGO_TANKER_CAVEAT
    ),
    (Category.TANKER, "medium"): "MR product/chemical tankers." + _CARGO_TANKER_CAVEAT,
    (Category.TANKER, "large"): "Aframax/Suezmax crude tankers, most LNG carriers." + _CARGO_TANKER_CAVEAT,
    (Category.TANKER, "very_large"): (
        "VLCC/ULCC and Q-Flex/Q-Max LNG carriers - the Q-Max's structural height is "
        "~34.7 m keel-to-deck (en.wikipedia.org/wiki/Q-Max), plus accommodation/mast "
        "above deck and the higher air draft typical of a ballast-condition transit. "
        "Needs verification: everything past the cited 34.7 m is this table's own "
        "estimate, not sourced."
        + _CARGO_TANKER_CAVEAT
    ),
    (Category.PASSENGER, "small"): "Harbor ferries, water taxis.",
    (Category.PASSENGER, "medium"): "Coastal ferries, small expedition cruise ships.",
    (Category.PASSENGER, "large"): "Mid-size cruise ships, RoPax ferries.",
    (Category.PASSENGER, "very_large"): (
        "Large cruise ships (~360 m+). Confirmed via Wikipedia: Oasis-class = 236 ft "
        "above waterline, Icon-class (e.g. Icon of the Seas) = 248 ft overall height - "
        "both exceed this bracket's old 230 ft figure, so it was raised to stay above "
        "the tallest confirmed class."
    ),
    (Category.FISHING, "any"): (
        "Factory trawlers rarely exceed this even when large. Needs verification: no "
        "authoritative source was found for large trawler mast/antenna height - this "
        "remains an unconfirmed domain-knowledge estimate."
    ),
    (Category.SMALL_WORKBOAT, "any"): (
        "Typical tug/pilot/SAR/port-tender wheelhouse height. Needs verification: no "
        "authoritative source was found for this figure."
    ),
    (Category.HIGH_SPEED_CRAFT, "any"): (
        "Low-profile catamaran/monohull ferries. Needs verification: no authoritative "
        "source was found for this figure."
    ),
}

_LOW_CONFIDENCE_CATEGORIES = {Category.CARGO, Category.TANKER}
_SINGLE_BRACKET_CATEGORIES = {Category.FISHING, Category.SMALL_WORKBOAT, Category.HIGH_SPEED_CRAFT}


def _estimate_for_category(category: Category, loa_m: float | None) -> HeightEstimate:
    if category == Category.MAST_DRIVEN:
        return HeightEstimate(
            None, category, None, None,
            "height is set by mast/rigging, not hull size - a table lookup by LOA "
            "doesn't apply; needs a per-vessel check if a figure is required",
        )
    if category == Category.UNSUPPORTED:
        return HeightEstimate(
            None, category, None, None,
            "AIS type code has no reference figure (too rare/exotic, or type not reported)",
        )

    if category in _SINGLE_BRACKET_CATEGORIES:
        height_ft = _HEIGHT_TABLE_FT[(category, "any")]
        return HeightEstimate(height_ft, category, "any", "medium", "single size-independent bracket")

    brackets = _CARGO_TANKER_BRACKETS if category in (Category.CARGO, Category.TANKER) else _PASSENGER_BRACKETS

    if loa_m is None:
        # AIS type arrived but Dimension.A/B are still 0/unset (see
        # ais_client._loa_from_dimension) - rather than withhold a verdict
        # until LOA shows up, which may never happen for a misconfigured
        # transponder, assume this category's largest bracket so a genuinely
        # tall vessel is never silently missed: a false FLAGGED is cheaper
        # than an unflagged one. bracket stays None (not the largest label)
        # since we don't actually know it - only height_ft is borrowed.
        largest_bracket = brackets[-1][1]
        height_ft = _HEIGHT_TABLE_FT[(category, largest_bracket)]
        return HeightEstimate(
            height_ft, category, None, "low",
            "no dimensions yet — assuming largest known size for this category",
        )

    bracket = _bracket_for_loa(loa_m, brackets)
    height_ft = _HEIGHT_TABLE_FT[(category, bracket)]
    confidence = "low" if category in _LOW_CONFIDENCE_CATEGORIES else "medium"
    note = (
        f"{category.value}/{bracket}: AIS type can't distinguish subtypes with very "
        "different height profiles at this size - value biased to the taller "
        "known subtype, so treat an OK verdict as provisional"
        if confidence == "low"
        else "direct table lookup by category and size bracket - AIS type code "
        "reliably identifies this category, so no subtype-ambiguity caveat applies"
    )
    return HeightEstimate(height_ft, category, bracket, confidence, note)


def estimate_height_ft(ais_type: int | None, loa_m: float | None) -> HeightEstimate:
    return _estimate_for_category(category_for_ais_type(ais_type), loa_m)


def estimate_height_from_web_category(category: Category, loa_m: float | None) -> HeightEstimate:
    """Same table lookup as estimate_height_ft, but starting from a category
    resolved via a web search (see vessel_name_lookup.resolve_vessel_name)
    rather than AIS's own type code - used when AIS has never reported a type
    for this vessel at all. Always confidence='low' when a figure comes back:
    a web search match is inherently less certain than AIS's own broadcast,
    on top of whatever bracket-level uncertainty the base estimate already
    carries, and the note always says so - superseded outright the moment a
    real ais_type arrives (see db.save_type_dimension clearing
    category_guessed)."""
    base = _estimate_for_category(category, loa_m)
    note = f"category sourced from a web search, not AIS ({base.note})"
    confidence = "low" if base.height_ft is not None else None
    return HeightEstimate(base.height_ft, base.category, base.bracket, confidence, note)


# --- Display helpers -------------------------------------------------------
# Everything below turns a stored (category, bracket, loa_m) triple back into
# the pieces a UI needs to show *why* a vessel landed on its height estimate -
# a readable category name, the bracket's actual size range, and precomputed
# layout data for drawing the size-bracket scale and the height-vs-threshold
# gauge. Kept string-keyed (not Category-enum-keyed) so callers can feed it
# straight from what's persisted in the database (est_height_category is
# stored as Category.value) without round-tripping through the enum.

CATEGORY_LABELS: dict[str, str] = {
    Category.CARGO.value: "Cargo",
    Category.TANKER.value: "Tanker",
    Category.PASSENGER.value: "Passenger",
    Category.FISHING.value: "Fishing",
    Category.SMALL_WORKBOAT.value: "Tug / pilot / SAR / workboat",
    Category.HIGH_SPEED_CRAFT.value: "High-speed craft",
    Category.MAST_DRIVEN.value: "Sailing / pleasure craft",
    Category.UNSUPPORTED.value: "Unknown / unsupported type",
}

_SIZED_BRACKETS_BY_CATEGORY: dict[str, list[tuple[float, str]]] = {
    Category.CARGO.value: _CARGO_TANKER_BRACKETS,
    Category.TANKER.value: _CARGO_TANKER_BRACKETS,
    Category.PASSENGER.value: _PASSENGER_BRACKETS,
}

# How far past the open-ended "very_large" bracket's own lower bound the
# visual size scale extends, purely so there's a finite bar to draw - the
# actual lookup stays unbounded above regardless of this number.
_DISPLAY_OVERHANG_M = 150.0


def brackets_for_category(category: str | None) -> list[tuple[str, float, float | None]] | None:
    """Ordered (label, lower_m, upper_m) brackets for a sized category -
    upper_m is None for the open-ended top bracket. None for categories that
    don't bracket by size at all (single-bracket or unscored types)."""
    bounds = _SIZED_BRACKETS_BY_CATEGORY.get(category) if category else None
    if not bounds:
        return None
    result = []
    lower = 0.0
    for upper, label in bounds:
        result.append((label, lower, None if upper == float("inf") else upper))
        lower = upper
    return result


def bracket_range_label(category: str | None, bracket: str | None) -> str | None:
    if not category or not bracket:
        return None
    brackets = brackets_for_category(category)
    if not brackets:
        return "size-independent"
    for label, lower, upper in brackets:
        if label == bracket:
            return f"≥ {lower:.0f} m" if upper is None else f"{lower:.0f}–{upper:.0f} m"
    return None


_SIZED_DISPLAY_ORDER = [Category.CARGO, Category.TANKER, Category.PASSENGER]
_SINGLE_BRACKET_DISPLAY_ORDER = [Category.FISHING, Category.SMALL_WORKBOAT, Category.HIGH_SPEED_CRAFT]


def full_table() -> list[dict]:
    """The complete type+size height table for display as a reference on the
    vessel detail page - independent of any particular vessel, unlike the
    other display helpers here which take a vessel's own category/bracket.

    Long format (one row per category+bracket) rather than a category x
    bracket grid, so each row has room for a "how was this derived" column -
    see _HEIGHT_TABLE_BASIS above."""
    rows = [
        {
            "category": category.value,
            "category_label": CATEGORY_LABELS[category.value],
            "bracket": label,
            "range_label": bracket_range_label(category.value, label),
            "height_ft": _HEIGHT_TABLE_FT[(category, label)],
            "basis": _HEIGHT_TABLE_BASIS[(category, label)],
        }
        for category in _SIZED_DISPLAY_ORDER
        for label, _lower, _upper in brackets_for_category(category.value)
    ]
    rows += [
        {
            "category": category.value,
            "category_label": CATEGORY_LABELS[category.value],
            "bracket": "any",
            "range_label": "size-independent",
            "height_ft": _HEIGHT_TABLE_FT[(category, "any")],
            "basis": _HEIGHT_TABLE_BASIS[(category, "any")],
        }
        for category in _SINGLE_BRACKET_DISPLAY_ORDER
    ]
    return rows


def size_scale(category: str | None, loa_m: float | None) -> dict | None:
    """Layout data for drawing the full size-bracket scale for `category`,
    with this vessel's own LOA marked on it - shows not just which bracket a
    vessel landed in, but where within (or how close to the edge of) it.
    None when the category has no size brackets, or LOA isn't known yet."""
    brackets = brackets_for_category(category)
    if not brackets or loa_m is None:
        return None
    display_max = brackets[-1][1] + _DISPLAY_OVERHANG_M  # very_large's lower bound + overhang
    segments = [
        {
            "label": label,
            "start_pct": round(lower / display_max * 100, 2),
            "end_pct": round((upper if upper is not None else display_max) / display_max * 100, 2),
        }
        for label, lower, upper in brackets
    ]
    return {
        "segments": segments,
        "marker_pct": round(min(loa_m, display_max) / display_max * 100, 2),
        "display_max_m": display_max,
        "loa_m": loa_m,
        "clamped": loa_m > display_max,
    }
