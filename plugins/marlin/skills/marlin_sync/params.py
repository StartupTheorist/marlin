#!/usr/bin/env python3
"""Shared, designer-owned constants for the consumer skill."""

import os

# Test hooks only: MARLIN_* scrubs to MARLIN_* publicly; in-repo tests use MARLIN_* safely.
WINDOW_SIZE = int(os.environ.get("MARLIN_WINDOW", "100"))
# Test hooks only: MARLIN_* scrubs to MARLIN_* publicly; in-repo tests use MARLIN_* safely.
CHANNEL_FLOOR = int(os.environ.get("MARLIN_CHANNEL_FLOOR", "10"))
# A channel keeps its floor protection only while its newest signal is this recent.
FLOOR_LIVENESS_DAYS = 21
# Ordinary urgent entries per channel; deadline-radar mandatory additions are separate.
URGENT_CAP = 5
# Default forward horizon for the deadline radar.
RADAR_HORIZON_DAYS = 21
# Deadline hits within this forward band become mandatory urgents in the finish pass.
MANDATORY_URGENT_DAYS = 14
# Recently passed deadlines remain visible on the radar for this many days.
DEADLINE_TAIL_DAYS = 7
# An acknowledged deadline re-escalates only inside this final window (Step 3).
ACK_FINAL_NAG_DAYS = 3
# Expired ack records leave the current-state store after this many days.
ACK_EXPIRED_PRUNE_DAYS = 90
# Archive history considered while proposing slow-accreting themes.
ARCHIVE_LOOKBACK_DAYS = 14
# Minimum total in-window plus archived members for a proposed theme birth.
THEME_BIRTH_MIN = 3
# A theme born within this age is mechanically emerging.
TREND_EMERGING_DAYS = 7
# A theme with no member this recent is mechanically fading.
THEME_FADING_DAYS = 7
# A theme with no member this recent is mechanically retired.
THEME_RETIRE_DAYS = 14
# Maximum brief-only, non-urgent notables per channel.
NOTABLE_CAP = 3
# Strictly greater membership turnover makes a theme eligible for a rename.
RENAME_TURNOVER = 0.5
# Signals cluster only when they share at least this many family-or-tag keys.
CLUSTER_ENTITY_OVERLAP_MIN = 1
# A resembles hint needs this much overlap with an existing theme (stricter than
# cluster edges: one incidental shared entity is not resemblance).
RESEMBLES_MIN_OVERLAP = 2
# Archived cluster members shown per candidate (titles only; prior_support has the rest).
CLUSTER_ARCHIVED_SAMPLE = 3


def public_params() -> dict[str, int | float]:
    """Return the shipped public parameter surface in a stable order."""
    return {
        name: value
        for name, value in sorted(globals().items())
        if name.isupper() and isinstance(value, (int, float))
    }


if __name__ == "__main__":
    for _name, _value in public_params().items():
        print(f"{_name}={_value}")
