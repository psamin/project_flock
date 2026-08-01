"""Recursive divergence cornering — the demo's money mechanic.

Checksum a range on both engines. If they agree, the whole subtree is clean and
we stop. If they differ, split and recurse only into the children that differ.
Cheap when nothing changed, precise where something did.

Still hard rule 1: both engines compute their own checksums and we compare two
integers per range. No rows cross the wire.
"""

from dataclasses import dataclass, field

from harness.checksum import range_checksum, split_range


@dataclass
class Cornering:
    leaves: list[tuple] = field(default_factory=list)  # diverging ranges, narrowed
    checks: int = 0                                    # checksum pairs executed
    max_depth: int = 0


def corner_divergence(
    source, target, table: str, pk_col: str, lo, hi,
    fanout: int = 8, leaf_size: int = 16, max_depth: int = 12,
) -> Cornering:
    """Narrow every divergence in [lo, hi) down to ranges of at most `leaf_size`."""
    result = Cornering()
    stack = [(lo, hi, 0)]
    while stack:
        rlo, rhi, depth = stack.pop()
        result.checks += 1
        result.max_depth = max(result.max_depth, depth)

        if range_checksum(source, table, pk_col, rlo, rhi) == range_checksum(
            target, table, pk_col, rlo, rhi
        ):
            continue  # clean subtree, and we never looked at a single row

        if rhi - rlo <= leaf_size or depth >= max_depth:
            result.leaves.append((rlo, rhi))
            continue

        stack.extend((clo, chi, depth + 1) for clo, chi in split_range(rlo, rhi, fanout))
    return result
