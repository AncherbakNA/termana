"""Pure, identity-based scopes for exporting immutable connection catalogs."""

from uuid import UUID

from termana.models.connection import ConnectionCatalog, ConnectionFolder, ConnectionProfile


def catalog_for_connection_export(
    catalog: ConnectionCatalog,
    item_id: UUID,
) -> ConnectionCatalog:
    """Promote one existing profile or complete folder subtree to the sole root.

    The source catalog already enforces size, depth, and tree invariants. Indexed
    lookups and an iterative walk therefore visit each scoped item only once,
    retaining persisted child order and sharing the original immutable records.
    Credential omission remains the portable encoder's responsibility.
    """
    if not isinstance(catalog, ConnectionCatalog):
        raise TypeError("catalog must be a ConnectionCatalog")
    if not isinstance(item_id, UUID):
        raise ValueError("Connection item ID must be a UUID")

    selected = catalog.folder_by_id(item_id) or catalog.profile_by_id(item_id)
    if selected is None:
        raise ValueError("Connection catalog item does not exist")

    folders: list[ConnectionFolder] = []
    profiles: list[ConnectionProfile] = []
    pending: list[ConnectionFolder | ConnectionProfile] = [selected]
    while pending:
        item = pending.pop()
        if isinstance(item, ConnectionFolder):
            folders.append(item)
            pending.extend(reversed(catalog.children_of(item.folder_id)))
        else:
            profiles.append(item)

    return ConnectionCatalog(
        root_item_ids=(item_id,),
        folders=tuple(folders),
        profiles=tuple(profiles),
    )
