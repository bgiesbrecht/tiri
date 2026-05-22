"""DbtMetadataProvider — reads metadata from a dbt manifest.json.

Stubbed for now. See docs/metadata.md for the field mapping spec
(description, column.description, is_primary_key inferred from
unique+not_null tests, recommended_joins from relationships tests).
Raises on enrich() until implemented.
"""

from __future__ import annotations

from tiri.data_models import RoomConfig, TableMeta
from tiri.providers.base import MetadataProvider, MetadataProviderError


class DbtMetadataProvider(MetadataProvider):
    def __init__(
        self,
        name: str,
        manifest_path: str,
        catalog_path: str | None = None,
    ) -> None:
        self._name = name
        self._manifest_path = manifest_path
        self._catalog_path = catalog_path

    @property
    def name(self) -> str:
        return self._name

    async def enrich(
        self,
        tables: dict[str, TableMeta],
        room_config: RoomConfig,
    ) -> None:
        raise MetadataProviderError(
            "DbtMetadataProvider not yet implemented — "
            "scheduled for a follow-up step"
        )
