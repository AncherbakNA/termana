"""Conservative three-way sync merge; conflicts NEVER yield a partial catalog."""

from dataclasses import dataclass
from uuid import UUID

from termana.profile_sync.model import SyncError, SyncRecord, SyncSnapshot, canonical_json

_ABSENT = object()
_DELETED = object()


@dataclass(frozen=True, slots=True)
class SyncConflict:
    item_id: UUID | None
    path: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class SyncMergeResult:
    snapshot: SyncSnapshot | None
    conflicts: tuple[SyncConflict, ...]


def _states(snapshot):
    result = {item.item_id: item for item in snapshot.records}
    result.update(dict.fromkeys(snapshot.tombstones, _DELETED))
    return result


def _validate_history(base, side):
    for item_id, old in base.items():
        current = side.get(item_id, _ABSENT)
        if current is _ABSENT:
            raise SyncError("Sync history lost a known identity without a tombstone")
        if old is _DELETED and current is not _DELETED:
            raise SyncError("A deleted sync identity cannot be reused")
        if (
            isinstance(old, SyncRecord)
            and isinstance(current, SyncRecord)
            and old.kind != current.kind
        ):
            raise SyncError("A sync identity cannot change record kind")


def merge_snapshots(
    base: SyncSnapshot, local: SyncSnapshot, remote: SyncSnapshot
) -> SyncMergeResult:
    """Merge disjoint fields; explicitly stop on edit/delete, reorder or ID conflicts.

    No timestamps/LWW, storage writes, remote commits or automatic reconnection.
    Sibling order is atomic for v1: two different reorder/add/remove operations
    on the SAME child list require a choice rather than silently losing order.
    """
    if any(type(item) is not SyncSnapshot for item in (base, local, remote)):
        raise SyncError("Three validated sync snapshots are required")
    if base.catalog_id != local.catalog_id or base.catalog_id != remote.catalog_id:
        raise SyncError("Sync snapshots belong to different catalogs")
    old, ours, theirs = (_states(item) for item in (base, local, remote))
    _validate_history(old, ours)
    _validate_history(old, theirs)
    conflicts = []

    def value_merge(before, left, right, item_id, path):
        if left == right:
            return left
        if left == before:
            return right
        if right == before:
            return left
        if (
            type(before) is dict
            and type(left) is dict
            and type(right) is dict
            and set(before) == set(left) == set(right)
        ):
            return {
                key: value_merge(before[key], left[key], right[key], item_id, (*path, key))
                for key in sorted(before)
            }
        conflicts.append(SyncConflict(item_id, path, "both-edited"))
        return left  # internal candidate only; NEVER returned when a conflict exists

    roots = value_merge(base.roots, local.roots, remote.roots, None, ("roots",))
    records, deleted = [], []
    for item_id in sorted(set(old) | set(ours) | set(theirs)):
        before, left, right = (side.get(item_id, _ABSENT) for side in (old, ours, theirs))
        if left == right:
            selected = left
        elif left == before:
            selected = right
        elif right == before:
            selected = left
        elif _DELETED in (left, right):
            conflicts.append(SyncConflict(item_id, (), "edit-delete"))
            continue
        elif all(isinstance(item, SyncRecord) for item in (before, left, right)):
            data = value_merge(before.document(), left.document(), right.document(), item_id, ())
            try:
                selected = SyncRecord(item_id, before.kind, canonical_json(data))
            except SyncError:
                conflicts.append(SyncConflict(item_id, (), "invalid-combination"))
                continue
        else:
            conflicts.append(SyncConflict(item_id, (), "identity-collision"))
            continue
        if selected is _DELETED:
            deleted.append(item_id)
        elif selected is not _ABSENT:
            records.append(selected)
    if conflicts:
        return SyncMergeResult(None, tuple(conflicts))
    try:
        merged = SyncSnapshot(base.catalog_id, roots, tuple(records), tuple(deleted))
    except SyncError:
        return SyncMergeResult(None, (SyncConflict(None, ("tree",), "invalid-combination"),))
    return SyncMergeResult(merged, ())
