from __future__ import annotations

from .scoring import ScoreProfile


# Generated from the organizer's redacted public scenario registry.
PUBLIC_SCORE_PROFILES = {
    "balanced-grid": ScoreProfile(16_648, 15_211, 963),
    "northbound-morning": ScoreProfile(36_398, 28_816, 1_250),
    "city-rush": ScoreProfile(83_137, 64_111, 2_297),
}


def score_profile(scenario_id: str) -> ScoreProfile:
    return PUBLIC_SCORE_PROFILES[scenario_id]
