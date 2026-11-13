"""Switching a central tag on, off or removing it for many subscriptions at once."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Literal

from app.config import SubTagActivation, save_subscriptions

if TYPE_CHECKING:
    from app.state import RuntimeState

Action = Literal["on", "off", "remove"]
ACTIONS = ("on", "off", "remove")


def apply_tag(state: "RuntimeState", sub_ids: Iterable[str], tag_id: str, action: Action) -> int:
    """Set tag ``tag_id`` on the given subscriptions; returns how many changed.

    on: active (added if missing) · off: kept but inactive (added inactive if
    missing, so the choice is visible) · remove: the assignment is deleted.
    The configuration is saved once, and the resolved-tag cache of the changed
    subscriptions is cleared so dynamic tags resolve again on the next poll.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if not any(t.id == tag_id for t in state.config.tags.tags):
        raise ValueError("unknown tag")
    wanted = set(sub_ids)
    changed: list[str] = []
    for sub in state.config.subscriptions.subscriptions:
        if sub.id not in wanted:
            continue
        existing = next((a for a in sub.active_tags if a.tag_id == tag_id), None)
        if action == "remove":
            if existing is None:
                continue
            sub.active_tags = [a for a in sub.active_tags if a.tag_id != tag_id]
        else:
            active = action == "on"
            if existing is None:
                sub.active_tags.append(SubTagActivation(tag_id=tag_id, active=active))
            elif existing.active == active:
                continue
            else:
                existing.active = active
        changed.append(sub.id)
    if changed:
        save_subscriptions(state.config.subscriptions, state.config_dir)
        engine = getattr(state, "polling_engine", None)
        if engine is not None:
            for sid in changed:
                engine._tag_cache.pop(sid, None)
    return len(changed)


def tag_usage(state: "RuntimeState") -> dict[str, int]:
    """tag id → number of subscriptions it is active on."""
    counts: dict[str, int] = {t.id: 0 for t in state.config.tags.tags}
    for sub in state.config.subscriptions.subscriptions:
        for a in sub.active_tags:
            if a.active and a.tag_id in counts:
                counts[a.tag_id] += 1
    return counts
