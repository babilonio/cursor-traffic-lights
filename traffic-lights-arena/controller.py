"""This is the only file you need to change."""


def control(state):
    """Return the requested green phase for every intersection."""
    phase = "NS_GREEN" if state["tick"] % 30 < 15 else "EW_GREEN"
    return {intersection_id: phase for intersection_id in state["intersections"]}
