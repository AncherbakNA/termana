"""Bounded, connection-free terminal pane layouts and their JSON representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

MAX_TERMINAL_PANES: Final = 16
MAX_PANE_LAYOUT_DEPTH: Final = MAX_TERMINAL_PANES - 1
MAX_PANE_LAYOUT_NODES: Final = 2 * MAX_TERMINAL_PANES - 1
DEFAULT_PANE_SPLIT_RATIO: Final = 5_000
MIN_PANE_SPLIT_RATIO: Final = 1
MAX_PANE_SPLIT_RATIO: Final = 9_999


class PaneOrientation(StrEnum):
    """Stable splitter axis identifiers, independent of Qt enums."""

    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"


@dataclass(frozen=True, slots=True)
class PaneLeaf:
    """One empty terminal pane, deliberately containing no session metadata."""


@dataclass(frozen=True, slots=True)
class PaneSplit:
    """Two ordered children with the first child's share in basis points."""

    orientation: PaneOrientation
    first: PaneLeaf | PaneSplit
    second: PaneLeaf | PaneSplit
    ratio: int = DEFAULT_PANE_SPLIT_RATIO

    def __post_init__(self) -> None:
        """Require a small immutable typed tree even for direct runtime callers."""
        _validated_pane_count(self)


@dataclass(frozen=True, slots=True)
class TerminalPaneLayout:
    """Pane structure and the active pane's zero-based depth-first position."""

    root: PaneLeaf | PaneSplit = field(default_factory=PaneLeaf)
    active_pane: int = 0

    def __post_init__(self) -> None:
        """Reject malformed trees and active indices outside their leaf range."""
        pane_count = _validated_pane_count(self.root)
        if type(self.active_pane) is not int:
            raise TypeError("active_pane must be an int")
        if not 0 <= self.active_pane < pane_count:
            raise ValueError("active_pane must identify an existing pane")

    @property
    def pane_count(self) -> int:
        """Return the bounded number of terminal leaves in this layout."""
        return _validated_pane_count(self.root)


def terminal_grid_layout(columns: int, rows: int) -> TerminalPaneLayout:
    """Build a bounded row-major grid using the existing persisted binary tree."""
    if type(columns) is not int or type(rows) is not int:
        raise TypeError("grid dimensions must be integers")
    if columns < 1 or rows < 1 or columns * rows > MAX_TERMINAL_PANES:
        raise ValueError(f"grid must contain between 1 and {MAX_TERMINAL_PANES} panes")

    def divide(nodes: list[PaneLeaf | PaneSplit], axis: PaneOrientation) -> PaneLeaf | PaneSplit:
        if len(nodes) == 1:
            return nodes[0]
        middle = len(nodes) // 2
        return PaneSplit(
            axis,
            divide(nodes[:middle], axis),
            divide(nodes[middle:], axis),
            round(middle * 10_000 / len(nodes)),
        )

    row_nodes = [
        divide([PaneLeaf() for _ in range(columns)], PaneOrientation.HORIZONTAL)
        for _ in range(rows)
    ]
    return TerminalPaneLayout(divide(row_nodes, PaneOrientation.VERTICAL))


def _validate_split_values(orientation: object, ratio: object) -> None:
    """Reject enum coercion and numeric lookalikes before rendering or storage."""
    if type(orientation) is not PaneOrientation:
        raise TypeError("orientation must be a PaneOrientation")
    if type(ratio) is not int:
        raise TypeError("split ratio must be an int")
    if not MIN_PANE_SPLIT_RATIO <= ratio <= MAX_PANE_SPLIT_RATIO:
        raise ValueError("split ratio must be between 1 and 9999 basis points")


def _validated_pane_count(root: object) -> int:
    """Check limits before descending, including for cyclic or forged objects."""
    nodes = 0
    panes = 0

    def visit(node: object, depth: int) -> None:
        nonlocal nodes, panes
        if depth > MAX_PANE_LAYOUT_DEPTH:
            raise ValueError("terminal pane layout exceeds the maximum depth")
        nodes += 1
        if nodes > MAX_PANE_LAYOUT_NODES:
            raise ValueError("terminal pane layout exceeds the maximum node count")
        if type(node) is PaneLeaf:
            panes += 1
            if panes > MAX_TERMINAL_PANES:
                raise ValueError("terminal pane layout exceeds the maximum pane count")
            return
        if type(node) is not PaneSplit:
            raise TypeError("pane nodes must be PaneLeaf or PaneSplit")
        _validate_split_values(node.orientation, node.ratio)
        visit(node.first, depth + 1)
        visit(node.second, depth + 1)

    visit(root, 0)
    return panes


def pane_layout_to_data(layout: TerminalPaneLayout) -> dict[str, object]:
    """Encode only layout structure, proportions, and an active leaf index."""
    if type(layout) is not TerminalPaneLayout:
        raise TypeError("layout must be TerminalPaneLayout")
    layout.__post_init__()

    def encode(node: PaneLeaf | PaneSplit) -> dict[str, object]:
        if type(node) is PaneLeaf:
            return {"kind": "pane"}
        return {
            "kind": "split",
            "orientation": node.orientation.value,
            "ratio": node.ratio,
            "first": encode(node.first),
            "second": encode(node.second),
        }

    return {"root": encode(layout.root), "active_pane": layout.active_pane}


def pane_layout_from_data(data: object) -> TerminalPaneLayout:
    """Decode an exact bounded JSON shape without accepting session metadata."""
    if type(data) is not dict:
        raise TypeError("terminal pane layout must be a JSON object")
    if len(data) != 2 or set(data) != {"root", "active_pane"}:
        raise ValueError("terminal pane layout fields do not match its schema")
    nodes = 0
    panes = 0

    def decode(node: object, depth: int) -> PaneLeaf | PaneSplit:
        nonlocal nodes, panes
        if depth > MAX_PANE_LAYOUT_DEPTH:
            raise ValueError("terminal pane layout exceeds the maximum depth")
        nodes += 1
        if nodes > MAX_PANE_LAYOUT_NODES:
            raise ValueError("terminal pane layout exceeds the maximum node count")
        if type(node) is not dict:
            raise TypeError("terminal pane node must be a JSON object")
        if node.get("kind") == "pane":
            if len(node) != 1 or set(node) != {"kind"}:
                raise ValueError("terminal pane leaf fields do not match its schema")
            panes += 1
            if panes > MAX_TERMINAL_PANES:
                raise ValueError("terminal pane layout exceeds the maximum pane count")
            return PaneLeaf()
        if node.get("kind") != "split":
            raise ValueError("unsupported terminal pane node kind")
        if len(node) != 5 or set(node) != {"kind", "orientation", "ratio", "first", "second"}:
            raise ValueError("terminal pane split fields do not match its schema")
        if type(node["orientation"]) is not str:
            raise TypeError("split orientation must be a string")
        orientation = PaneOrientation(node["orientation"])
        _validate_split_values(orientation, node["ratio"])
        return PaneSplit(
            orientation=orientation,
            first=decode(node["first"], depth + 1),
            second=decode(node["second"], depth + 1),
            ratio=node["ratio"],
        )

    return TerminalPaneLayout(root=decode(data["root"], 0), active_pane=data["active_pane"])
