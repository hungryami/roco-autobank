"""Daily scene-item pickup definitions from the Kotlin client."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PickupItem:
    item_id: int
    condition: int
    item_type: int
    scene_id: int
    score: int = 0


@dataclass(frozen=True, slots=True)
class PickupGroup:
    key: str
    name: str
    items: tuple[PickupItem, ...]


PICKUP_GROUPS: tuple[PickupGroup, ...] = (
    PickupGroup(
        "magic_stone",
        "魔法石",
        (
            PickupItem(4, 1, 1, 19), PickupItem(122, 1, 1, 125),
            PickupItem(131, 1, 1, 137), PickupItem(165, 1, 1, 198),
            PickupItem(1, 1, 1, 7), PickupItem(86, 1, 1, 75),
            PickupItem(85, 1, 1, 74), PickupItem(45, 1, 1, 28),
            PickupItem(9, 1, 1, 11), PickupItem(308, 1, 1, 222),
            PickupItem(128, 1, 1, 134), PickupItem(84, 1, 1, 73),
            PickupItem(5, 1, 1, 17), PickupItem(44, 1, 1, 16),
            PickupItem(129, 1, 1, 135), PickupItem(145, 1, 1, 165),
            PickupItem(146, 1, 1, 166), PickupItem(108, 1, 1, 93),
            PickupItem(10, 2, 1, 18), PickupItem(155, 1, 1, 179),
            PickupItem(160, 1, 1, 183), PickupItem(162, 1, 1, 185),
            PickupItem(106, 1, 1, 87), PickupItem(107, 1, 1, 87),
            PickupItem(11, 2, 1, 62),
        ),
    ),
    PickupGroup(
        "five_spirit_stones",
        "五灵石",
        (
            PickupItem(168, 1, 1, 203), PickupItem(302, 1, 1, 26),
            PickupItem(164, 1, 1, 194), PickupItem(309, 1, 1, 287),
            PickupItem(132, 1, 1, 140), PickupItem(150, 1, 1, 169),
            PickupItem(149, 1, 1, 168), PickupItem(24, 1, 1, 11),
            PickupItem(42, 1, 1, 25), PickupItem(305, 1, 1, 214),
            PickupItem(306, 1, 1, 10), PickupItem(17, 1, 1, 19),
            PickupItem(31, 1, 1, 17), PickupItem(139, 1, 1, 148),
            PickupItem(161, 1, 1, 183), PickupItem(43, 1, 1, 24),
            PickupItem(38, 1, 1, 18),
        ),
    ),
    PickupGroup(
        "magic_spirit_stone",
        "魔法灵石",
        (PickupItem(126, 1, 1, 128), PickupItem(166, 1, 1, 202)),
    ),
    PickupGroup(
        "water_drop",
        "水滴",
        (
            PickupItem(119, 1, 1, 111), PickupItem(110, 1, 1, 97),
            PickupItem(303, 1, 1, 26), PickupItem(54, 1, 1, 66),
        ),
    ),
    PickupGroup(
        "flower",
        "花朵",
        (
            PickupItem(37, 1, 1, 66), PickupItem(114, 1, 1, 103),
            PickupItem(138, 1, 1, 147),
        ),
    ),
    PickupGroup(
        "misc",
        "杂项",
        (
            PickupItem(21, 1, 1, 18), PickupItem(154, 1, 1, 174),
            PickupItem(152, 2, 1, 171), PickupItem(312, 1, 1, 342),
        ),
    ),
)

PICKUP_GROUPS_BY_KEY = {group.key: group for group in PICKUP_GROUPS}
