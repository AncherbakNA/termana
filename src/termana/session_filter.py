"""Pure matching rules for the saved-connections catalog."""

import unicodedata
from dataclasses import dataclass
from uuid import UUID

from termana.models.connection import (
    ConnectionCatalog,
    ConnectionFolder,
    ConnectionProfile,
    ConnectionProtocol,
)
from termana.telnet_encoding_labels import telnet_encoding_search_aliases

DomainItem = ConnectionFolder | ConnectionProfile


@dataclass(frozen=True, slots=True)
class SessionFilterProjection:
    """Stable IDs visible in and directly matched by one filter query."""

    visible_item_ids: frozenset[UUID]
    matching_item_ids: frozenset[UUID]


def normalize_session_filter_text(value: str) -> str:
    """Return a compatibility-normalized, caseless, single-spaced search value."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def session_filter_terms(value: str) -> tuple[str, ...]:
    """Split a normalized query into terms that must all match one item."""
    normalized_value = normalize_session_filter_text(value)
    return tuple(normalized_value.split(" ")) if normalized_value else ()


@dataclass(frozen=True, slots=True)
class SessionFilterIndex:
    """Reusable normalized documents for one immutable catalog snapshot."""

    _catalog: ConnectionCatalog
    _search_documents: tuple[
        tuple[UUID, tuple[str, ...], tuple[str, ...]],
        ...,
    ]
    _favorite_profile_ids: frozenset[UUID]

    @classmethod
    def from_catalog(cls, catalog: ConnectionCatalog) -> "SessionFilterIndex":
        """Build the index once when the coordinator publishes a new catalog."""
        documents: list[tuple[UUID, tuple[str, ...], tuple[str, ...]]] = []

        def visit(
            domain_item: DomainItem,
            ancestor_names: tuple[str, ...],
            ancestor_paths: tuple[str, ...],
        ) -> None:
            if isinstance(domain_item, ConnectionFolder):
                item_path_names = (*ancestor_names, domain_item.name)
                normalized_name = normalize_session_filter_text(domain_item.name)
                item_path = normalize_session_filter_text(" / ".join(item_path_names))
                compact_item_path = normalize_session_filter_text("/".join(item_path_names))
                searchable_fields = (
                    normalized_name,
                    item_path,
                    compact_item_path,
                )
                searchable_paths: tuple[str, ...] = ()
            else:
                searchable_fields = _profile_searchable_fields(domain_item)
                searchable_paths = ancestor_paths

            documents.append(
                (
                    _domain_item_id(domain_item),
                    searchable_fields,
                    searchable_paths,
                )
            )
            if isinstance(domain_item, ConnectionFolder):
                child_ancestor_paths = (item_path, compact_item_path)
                for child in catalog.children_of(domain_item.folder_id):
                    visit(child, item_path_names, child_ancestor_paths)

        for root_item in catalog.children_of(None):
            visit(root_item, (), ())
        return cls(
            catalog,
            tuple(documents),
            frozenset(profile.profile_id for profile in catalog.profiles if profile.is_favorite),
        )

    def project(
        self,
        terms: tuple[str, ...],
        *,
        favorites_only: bool = False,
    ) -> SessionFilterProjection:
        """Match terms, optionally retaining only favorite profiles and their ancestors.

        In favorites mode a matched folder still contributes its descendants, but
        only favorite profiles survive. Matching IDs then count those profiles,
        not the folders retained solely to preserve their catalog context.
        """
        if type(favorites_only) is not bool:
            raise TypeError("favorites_only must be a bool")
        if not terms:
            if favorites_only:
                return self._favorite_projection(self._favorite_profile_ids)
            all_item_ids = frozenset(item_id for item_id, _fields, _paths in self._search_documents)
            return SessionFilterProjection(all_item_ids, all_item_ids)

        path_match_cache: dict[tuple[tuple[str, ...], str], bool] = {}

        def document_matches(
            searchable_fields: tuple[str, ...],
            searchable_paths: tuple[str, ...],
        ) -> bool:
            for term in terms:
                if any(term in field for field in searchable_fields):
                    continue
                cache_key = searchable_paths, term
                path_matches = path_match_cache.get(cache_key)
                if path_matches is None:
                    path_matches = any(term in path for path in searchable_paths)
                    path_match_cache[cache_key] = path_matches
                if not path_matches:
                    return False
            return True

        matching_item_ids = {
            item_id
            for item_id, searchable_fields, searchable_paths in self._search_documents
            if document_matches(searchable_fields, searchable_paths)
        }
        visible_item_ids = set(matching_item_ids)

        expanded_folder_ids: set[UUID] = set()
        expanded_ancestor_ids: set[UUID] = set()

        def add_folder_subtree(folder_id: UUID) -> None:
            if folder_id in expanded_folder_ids:
                return
            expanded_folder_ids.add(folder_id)
            for child in self._catalog.children_of(folder_id):
                child_id = _domain_item_id(child)
                visible_item_ids.add(child_id)
                if isinstance(child, ConnectionFolder):
                    add_folder_subtree(child.folder_id)

        for item_id in matching_item_ids:
            parent_id = self._catalog.parent_id_of(item_id)
            while parent_id is not None:
                if parent_id in expanded_ancestor_ids:
                    break
                expanded_ancestor_ids.add(parent_id)
                visible_item_ids.add(parent_id)
                parent_id = self._catalog.parent_id_of(parent_id)
            if self._catalog.folder_by_id(item_id) is not None:
                add_folder_subtree(item_id)

        if favorites_only:
            return self._favorite_projection(visible_item_ids & self._favorite_profile_ids)
        return SessionFilterProjection(
            frozenset(visible_item_ids),
            frozenset(matching_item_ids),
        )

    def _favorite_projection(
        self,
        matching_profile_ids: set[UUID] | frozenset[UUID],
    ) -> SessionFilterProjection:
        """Retain each ancestor once, keeping work linear even for shared paths."""
        visible_item_ids = set(matching_profile_ids)
        for profile_id in matching_profile_ids:
            parent_id = self._catalog.parent_id_of(profile_id)
            while parent_id is not None and parent_id not in visible_item_ids:
                visible_item_ids.add(parent_id)
                parent_id = self._catalog.parent_id_of(parent_id)
        return SessionFilterProjection(
            frozenset(visible_item_ids),
            frozenset(matching_profile_ids),
        )


def catalog_filter_projection(
    catalog: ConnectionCatalog,
    terms: tuple[str, ...],
    *,
    favorites_only: bool = False,
) -> SessionFilterProjection:
    """Return a catalog projection without changing domain or UI state.

    Every query term must occur in the normalized searchable fields of an item.
    Ancestors of matching descendants remain visible. A folder that directly
    matches deliberately contributes its complete subtree. Favorites mode
    intersects that result with favorite profiles and keeps their ancestors.
    """
    return SessionFilterIndex.from_catalog(catalog).project(terms, favorites_only=favorites_only)


def _profile_searchable_fields(
    profile: ConnectionProfile,
) -> tuple[str, ...]:
    """Build normalized endpoint fields independent of shared folder paths."""
    endpoint = f"{profile.host}:{profile.port}"
    telnet_encoding_fields = (
        tuple(
            normalize_session_filter_text(alias)
            for alias in telnet_encoding_search_aliases(profile.telnet_encoding)
        )
        if profile.protocol is ConnectionProtocol.TELNET
        else ()
    )
    return (
        normalize_session_filter_text(profile.name),
        normalize_session_filter_text(profile.protocol.value),
        *telnet_encoding_fields,
        normalize_session_filter_text(profile.host),
        str(profile.port),
        normalize_session_filter_text(endpoint),
    )


def _domain_item_id(item: DomainItem) -> UUID:
    """Return the stable identity shared by catalog and tree projections."""
    if isinstance(item, ConnectionFolder):
        return item.folder_id
    return item.profile_id
