"""Compile cross-field rules onto a :class:`Layout`.

A cross-field rule compares two elementary leaves.  When either leaf lives
under an ``OCCURS`` ancestor the bare field name is ambiguous: the same name
can denote several physical leaves (different OCCURS instances, different
ancestor groups, or REDEFINES alternatives).  This module resolves that
ambiguity once, when the contract is loaded, instead of guessing at runtime.

The produced :class:`CrossBinding` carries the stable, verifiable coordinates
of every aligned pair, so :mod:`copybook_kit.rules.checks` never zips flat
lists or silently takes the first same-named leaf.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import RulesError
from ..model import Layout, LeafOccurrence

# Flat paths look like ``GROUP[0].CHILD[2]``; rule selectors may use the same
# shape with ``[*]`` wildcards on the OCCURS axes.  Only leaf names may be
# selected, so a selector always ends in an elementary field name.
_SEGMENT_RE = re.compile(r"^(?P<name>[A-Z][A-Z0-9-]*)(?:\[(?P<index>\d+|\*)\])?$")
_INDEX_RE = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class SideBinding:
    """One operand of a cross-field rule bound to physical layout leaves.

    ``paths`` is the ordered list of flat paths the operand projects to.
    ``coords`` is the OCCURS coordinate tuple of each path in the same order;
    an empty tuple means the operand is a scalar outside every OCCURS group.
    ``offsets`` maps each path to its record-relative byte offset and length.
    ``template`` is the selector path with indices replaced by ``*``, e.g.
    ``PAIR[*].LOW``; it names the physical OCCURS ancestors of the operand.
    """

    name: str
    selector: str
    paths: tuple[str, ...]
    coords: tuple[tuple[int, ...], ...]
    offsets: dict[str, tuple[int, int]]
    template: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrossBinding:
    """Resolved, ambiguity-free alignment plan for one cross-field rule."""

    rule_name: str
    op: str
    quantifier: str | None
    left: SideBinding
    right: SideBinding
    # Prefix length shared by both sides: the first ``align_axes`` indices of
    # every coordinate are the pairing key.  Beyond it, one side (the shorter
    # coordinate) broadcasts to every index of the other side.
    align_axes: int

    @property
    def is_scalar_pair(self) -> bool:
        return self.quantifier is None

    def coordinates(self) -> list[tuple[int, ...]]:
        """All alignment coordinate tuples, in stable sorted order.

        The universe is the full rectangle at the deepest OCCURS depth of
        either side; a shallower side (fewer axes) broadcasts over the extra
        inner axes.  When both sides are scalars the single empty tuple is the
        only coordinate.
        """
        max_depth = max((len(side.coords[0]) if side.coords else 0)
                        for side in (self.left, self.right))
        dims: list[int] = []
        for axis in range(max_depth):
            sizes = {coord[axis] for side in (self.left, self.right)
                     for coord in side.coords if len(coord) > axis}
            dims.append((max(sizes) + 1) if sizes else 1)
        if not dims:
            return [()]
        return sorted(_cartesian(tuple(range(dim) for dim in dims)))

    def paths_at(self, side: SideBinding, coord: tuple[int, ...]) -> list[str]:
        if not side.coords:
            return list(side.paths)
        # Compare the shared prefix; an inner-broadcast side matches every
        # index beyond its own depth (same prefix, shorter coordinate).
        return [path for path, path_coord in zip(side.paths, side.coords)
                if path_coord[: len(coord)] == coord[: len(path_coord)]]


def parse_selector(text: str) -> tuple[tuple[str, int | None], ...]:
    """Parse a selector such as ``PAIR[*].LOW`` or ``LOW`` into segments.

    A wildcard ``[*]`` and a segment without brackets both parse to index
    ``None`` (match any index / no OCCURS axis); a literal ``[n]`` pins the
    segment to occurrence ``n``.
    """
    if not text:
        raise RulesError("cross-field path selector must not be empty", code="RC003")
    segments: list[tuple[str, int | None]] = []
    for segment in text.split("."):
        match = _SEGMENT_RE.fullmatch(segment)
        if match is None:
            raise RulesError(f"invalid cross-field path selector {text!r}", code="RC003")
        index_text = match.group("index")
        index = None if index_text in (None, "*") else int(index_text)
        segments.append((match.group("name"), index))
    if not segments:
        raise RulesError(f"invalid cross-field path selector {text!r}", code="RC003")
    return tuple(segments)


def _leaf_coords(leaf: LeafOccurrence) -> tuple[int, ...]:
    return tuple(int(item) for item in _INDEX_RE.findall(leaf.path))


def _runtime_path(leaf: LeafOccurrence) -> str:
    """Flat-path key as emitted by ``decode_record``.

    Decoding starts inside the single 01 record (see ``decode_record.walk``),
    so runtime flat keys omit the 01 root name while layout leaf paths include
    it.  Strip exactly that root prefix to keep both coordinates in one place.
    """
    root = leaf.path.split(".", 1)[0]
    if "." in leaf.path and "[" not in root:
        return leaf.path.split(".", 1)[1]
    return leaf.path


def _matches(leaf: LeafOccurrence, leaf_name: str,
             selector: tuple[tuple[str, int | None], ...]) -> bool:
    path_segments = parse_selector(leaf.path)
    if path_segments[-1][0] != leaf_name:
        return False
    # A bare leaf name (or any shorter selector) anchors at the end of the
    # path; an explicit multi-segment selector must cover the full path.
    if len(selector) > len(path_segments):
        return False
    aligned = path_segments[-len(selector):]
    for (actual_name, actual_index), (wanted_name, wanted_index) in zip(aligned, selector):
        if actual_name != wanted_name:
            return False
        if wanted_index is not None and actual_index != wanted_index:
            return False
    return True


def _bind_side(layout: Layout, rule_name: str, label: str, leaf_name: str,
               selector_text: str | None) -> SideBinding:
    leaves = [leaf for leaf in layout.leaves() if leaf.field.name == leaf_name]
    if not leaves:
        raise RulesError(f"record rule {rule_name} references unknown field {leaf_name}", code="RC004")
    selector = parse_selector(selector_text or leaf_name)
    if selector[-1][0] != leaf_name:
        raise RulesError(
            f"record rule {rule_name}: {label}_path must select the {label} leaf {leaf_name!r}",
            code="RC003")
    matched = [leaf for leaf in leaves if _matches(leaf, leaf_name, selector)]
    if not matched:
        raise RulesError(
            f"record rule {rule_name}: {label}_path {selector_text!r} does not match any leaf "
            f"named {leaf_name}", code="RC004")
    if len({_runtime_path(leaf) for leaf in matched}) != len(matched):
        raise RulesError(f"record rule {rule_name}: selector {selector_text!r} matched a leaf twice",
                         code="RC003")
    runtime_paths = tuple(_runtime_path(leaf) for leaf in matched)
    template = tuple(segment[0] for segment in parse_selector(matched[0].path))
    return SideBinding(
        name=leaf_name,
        selector=selector_text or leaf_name,
        paths=runtime_paths,
        coords=tuple(_leaf_coords(leaf) for leaf in matched),
        offsets={_runtime_path(leaf): (leaf.offset, leaf.length) for leaf in matched},
        template=template,
    )


def _declared_dims(leaves: list[LeafOccurrence], width: int) -> tuple[int, ...]:
    """OCCURS count per axis from the layout's full leaf population."""
    dims = [0] * width
    for leaf in leaves:
        coord = _leaf_coords(leaf)
        for axis, index in enumerate(coord):
            dims[axis] = max(dims[axis], index + 1)
    return tuple(dims)


def _check_shape(rule_name: str, label: str, side: SideBinding,
                 same_name_leaves: list[LeafOccurrence]) -> None:
    if not side.coords:
        return
    width = len(side.coords[0])
    if any(len(coord) != width for coord in side.coords):
        raise RulesError(f"record rule {rule_name}: {label} leaf {side.name!r} has inconsistent OCCURS "
                         "coordinates", code="RC003")
    # Every leaf sharing the name lives at the same OCCURS depth; the declared
    # axis sizes come from that full population, not from this projection.
    dims = _declared_dims(same_name_leaves, width)
    full = tuple(range(dim) for dim in dims)
    expected = {tuple(item) for item in _cartesian(full)}
    actual = set(side.coords)
    if not actual or not actual <= expected or len(side.coords) != len(actual):
        raise RulesError(
            f"record rule {rule_name}: {label}_path {side.selector!r} does not project a complete "
            f"OCCURS rectangle for {side.name!r}", code="RC003")


def _cartesian(axes: tuple[list[int], ...]) -> list[tuple[int, ...]]:
    if not axes:
        return [()]
    rest = _cartesian(axes[1:])
    return [(index,) + tail for index in axes[0] for tail in rest]


def _ambiguous_message(rule_name: str, label: str, leaf_name: str,
                       leaves: list[LeafOccurrence]) -> RulesError:
    paths = sorted({leaf.path for leaf in leaves})
    rendered = ", ".join(paths)
    return RulesError(
        f"record rule {rule_name}: {label} leaf {leaf_name!r} is ambiguous; {len(paths)} physical "
        f"paths match ({rendered}). Declare {label}_path with [*] coordinates to disambiguate",
        code="RC003")


def compile_cross_binding(layout: Layout, rule_name: str, op: str, quantifier: str | None,
                          left_name: str, right_name: str,
                          left_selector: str | None, right_selector: str | None) -> CrossBinding:
    """Resolve one cross-field rule against the compiled layout or raise."""
    left_leaves = [leaf for leaf in layout.leaves() if leaf.field.name == left_name]
    right_leaves = [leaf for leaf in layout.leaves() if leaf.field.name == right_name]
    if not left_leaves:
        raise RulesError(f"record rule {rule_name} references unknown field {left_name}", code="RC004")
    if not right_leaves:
        raise RulesError(f"record rule {rule_name} references unknown field {right_name}", code="RC004")
    repeated_left = any(_leaf_coords(leaf) for leaf in left_leaves)
    repeated_right = any(_leaf_coords(leaf) for leaf in right_leaves)
    if not repeated_left and not repeated_right and len(left_leaves) == 1 and len(right_leaves) == 1:
        if quantifier is not None:
            raise RulesError(
                f"record rule {rule_name}: quantifier is only valid for OCCURS-aligned leaves",
                code="RC003")
        if left_selector is not None or right_selector is not None:
            raise RulesError(
                f"record rule {rule_name}: *_path selectors require an OCCURS ancestor", code="RC003")
    if repeated_left or repeated_right:
        if quantifier is None:
            raise RulesError(
                f"record rule {rule_name}: {left_name!r}/{right_name!r} live under OCCURS; declare "
                "quantifier all_aligned or any_aligned and, when needed, explicit *_path selectors",
                code="RC003")
    # Same name with several physical paths is only ambiguous when the paths
    # diverge above the leaf (different OCCURS ancestors or REDEFINES
    # alternatives).  Several occurrences of one aligned OCCURS structure
    # share their ancestor templates and are paired by index instead.
    for label, name, selector, leaves in (("left", left_name, left_selector, left_leaves),
                                          ("right", right_name, right_selector, right_leaves)):
        distinct = {leaf.path for leaf in leaves}
        if selector is None and len(distinct) > 1:
            templates = {tuple(segment[0] for segment in parse_selector(leaf.path)[:-1])
                         for leaf in leaves}
            if len(templates) > 1:
                raise _ambiguous_message(rule_name, label, name, leaves)
    left = _bind_side(layout, rule_name, "left", left_name, left_selector)
    right = _bind_side(layout, rule_name, "right", right_name, right_selector)
    _check_shape(rule_name, "left", left, left_leaves)
    _check_shape(rule_name, "right", right, right_leaves)
    align_axes = _pairing_axes(rule_name, left, right)
    return CrossBinding(rule_name, op, quantifier, left, right, align_axes)


def _pairing_axes(rule_name: str, left: SideBinding, right: SideBinding) -> int:
    """Validate the OCCURS-ancestor relationship and return the pairing depth."""
    if not left.coords or not right.coords:
        # One side is a scalar outside every OCCURS group: broadcast.
        return len(left.coords[0]) if left.coords else len(right.coords[0])
    left_depth, right_depth = len(left.coords[0]), len(right.coords[0])
    shared = min(left_depth, right_depth)
    for axis in range(shared):
        left_axis = {coord[axis] for coord in left.coords}
        right_axis = {coord[axis] for coord in right.coords}
        if left_axis != right_axis:
            raise RulesError(
                f"record rule {rule_name}: {left.name!r} and {right.name!r} have incompatible "
                f"OCCURS axes at position {axis}; aligned comparisons require the same ancestor",
                code="RC003")
    # Shared prefix must denote the *same* OCCURS ancestors.  Two leaves whose
    # coordinate values happen to line up but which live under differently
    # named groups (distinct repeated ancestors) are not alignable.  The
    # template is ``(root, outer group, ..., inner group, leaf name)`` and the
    # OCCURS axis at depth ``axis`` is the segment at ``-(width - axis) - 1``.
    for axis in range(shared):
        left_group = left.template[-(left_depth - axis) - 1]
        right_group = right.template[-(right_depth - axis) - 1]
        if left_group != right_group:
            conflicts = sorted(set(left.paths) | set(right.paths))
            raise RulesError(
                f"record rule {rule_name}: {left.name!r} and {right.name!r} come from distinct OCCURS "
                f"ancestors and cannot be paired by index. Conflicting paths: {', '.join(conflicts)}",
                code="RC003")
    return shared
