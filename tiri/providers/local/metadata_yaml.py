"""YAMLMetadataProvider — human-authored metadata in a YAML file.

See docs/metadata.md for the format. Behaviorally identical to
`StaticMetadataProvider` once parsed; this provider exists to load the file
and delegate.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tiri.data_models import RoomConfig, TableMeta
from tiri.providers.base import MetadataProvider, MetadataProviderError
from tiri.providers.local.metadata_static import StaticMetadataProvider


class YAMLMetadataProvider(MetadataProvider):
    def __init__(self, name: str, path: str) -> None:
        self._name = name
        self._path = path
        self._delegate = self._load(name, path)

    @property
    def name(self) -> str:
        return self._name

    async def enrich(
        self,
        tables: dict[str, TableMeta],
        room_config: RoomConfig,
    ) -> None:
        await self._delegate.enrich(tables, room_config)

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _load(name: str, path: str) -> StaticMetadataProvider:
        file = Path(path)
        if not file.exists():
            raise MetadataProviderError(
                f"YAMLMetadataProvider {name!r}: file not found: {path}"
            )
        try:
            with file.open() as f:
                root = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise MetadataProviderError(
                f"YAMLMetadataProvider {name!r}: invalid YAML: {e}"
            ) from e
        if not isinstance(root, dict):
            raise MetadataProviderError(
                f"YAMLMetadataProvider {name!r}: root must be a mapping; "
                f"got {type(root).__name__}"
            )
        data = root.get("tables") or {}
        if not isinstance(data, dict):
            raise MetadataProviderError(
                f"YAMLMetadataProvider {name!r}: `tables` must be a mapping"
            )
        return StaticMetadataProvider(name=name, data=data)
