"""
mars_zones.py — Living heart-rate zones anchored to threshold tests
===================================================================

The founding EPOCH story: an athlete rode for years "with the handbrake on"
because the watch set the wrong zones. The fix is to anchor zones to a real
THRESHOLD TEST, and let them MOVE as the athlete is re-tested over time.

This module is pure (no DB). It turns a threshold heart rate (LTHR, from a
field/FTP test) into the full Mars zone set. The zone STRUCTURE is preserved;
only the anchor moves. When the athlete tests again and their threshold rises,
every zone shifts up with it — no more handbrake.

Zone tops are stored as % of LTHR, calibrated so the athlete's CURRENT
threshold reproduces their CURRENT Mars zones exactly (see self-test).
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Zone tops as a fraction of LTHR. Calibrated from the official Mars zones so
# that lt_bpm = 168 (cycling) / 173 (running) reproduces today's zones exactly.
# Order: z1_top, transition_top, z2_top, z3_top, z4_top. z5 is everything above.
ZONE_PCT = {
    "cycling": [0.643, 0.792, 0.893, 0.952, 1.000],
    "running": [0.647, 0.792, 0.890, 0.948, 1.000],
}
ZONE_KEYS = ["z1", "transition", "z2", "z3", "z4"]
ZONE_LABELS = {
    "z1": "Z1 Recovery",
    "transition": "Transition",
    "z2": "Z2 Aerobic",
    "z3": "Z3 Tempo",
    "z4": "Z4 Threshold",
    "z5": "Z5 Maximum",
}


def _sport_key(sport: str) -> str:
    return "running" if str(sport).lower().startswith("run") else "cycling"


def zones_from_threshold(
    lt_bpm: int,
    sport: str = "cycling",
    max_hr: Optional[int] = None,
) -> Dict[str, object]:
    """Build the full HR zone set from a threshold heart rate (LTHR).

    Args:
        lt_bpm: lactate threshold heart rate from a test (the anchor).
        sport: "cycling" or "running".
        max_hr: optional max HR; caps the top of Z5.

    Returns:
        { sport, lt_bpm, max_hr, method, zones: {z1:[lo,hi], ... z5:[lo,hi]} }
    """
    pct = ZONE_PCT[_sport_key(sport)]
    tops = [round(lt_bpm * p) for p in pct]

    zones: Dict[str, List[int]] = {}
    lo = 0
    for key, top in zip(ZONE_KEYS, tops):
        zones[key] = [lo, top]
        lo = top + 1
    zones["z5"] = [lo, max_hr or 999]

    return {
        "sport": sport,
        "lt_bpm": lt_bpm,
        "max_hr": max_hr,
        "method": "field_test_lthr",
        "zones": zones,
    }


def describe_zone_shift(old_lt: int, new_lt: int, sport: str = "cycling") -> Dict[str, object]:
    """Compare two anchors so the athlete can SEE their zones move (no handbrake)."""
    old = zones_from_threshold(old_lt, sport)["zones"]
    new = zones_from_threshold(new_lt, sport)["zones"]
    shifts = {k: [new[k][0] - old[k][0], new[k][1] - old[k][1]] for k in old}
    direction = "up" if new_lt > old_lt else ("down" if new_lt < old_lt else "same")
    return {
        "old_lt": old_lt,
        "new_lt": new_lt,
        "delta_bpm": new_lt - old_lt,
        "direction": direction,
        "old_zones": old,
        "new_zones": new,
        "per_zone_shift": shifts,
    }


# --- Self-test: must reproduce today's Mars zones exactly -------------------

if __name__ == "__main__":
    # Today's official Mars cycling zones (lt 168):
    #   z1 0-108 · transition 109-133 · z2 134-150 · z3 151-160 · z4 161-168 · z5 169+
    cyc = zones_from_threshold(168, "cycling")["zones"]
    print("cycling @168:", cyc)
    expected = {"z1": [0, 108], "transition": [109, 133], "z2": [134, 150],
                "z3": [151, 160], "z4": [161, 168], "z5": [169, 999]}
    print("reproduces Mars zones exactly:", cyc == expected)

    run = zones_from_threshold(173, "running")["zones"]
    print("running  @173:", run)

    # What happens when a future test says the threshold moved up to 172:
    shift = describe_zone_shift(168, 172, "cycling")
    print("\nIf your next test reads LTHR 172 (up 4 bpm):")
    for k in ["z2", "z4"]:
        print(f"  {k}: {shift['old_zones'][k]} -> {shift['new_zones'][k]}")
