from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime

import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.catalog.adapter import McpRegistryV01Adapter, NormalizedMcpArtifact
from app.catalog.models import CatalogArtifact, CatalogBinding, CatalogSource
from app.config.settings import settings
from app.mcp.lifecycle import mcp_lifecycle_store

logger = structlog.get_logger()


class CatalogStore:
    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_source(
        self,
        *,
        source_id: object | None = None,
        workspace_id: str,
        display_name: str,
        base_url: str,
        auth_type: str,
        auth_secret_name: str | None,
        auth_header_name: str | None,
        network_route: str,
        enabled: bool,
        management_mode: str,
        artifact_kind: str,
        adapter_type: str,
        adapter_base_path: str,
        credential_transitioning: bool = False,
    ) -> tuple[CatalogSource, CatalogBinding]:
        async with self.async_session() as session:
            source = CatalogSource(
                workspace_id=workspace_id,
                display_name=display_name,
                base_url=base_url,
                auth_type=auth_type,
                auth_secret_name=auth_secret_name,
                auth_header_name=auth_header_name,
                credential_transitioning=credential_transitioning,
                network_route=network_route,
                enabled=enabled,
                management_mode=management_mode,
            )
            if source_id is not None:
                source.id = source_id
            session.add(source)
            await session.flush()
            binding = CatalogBinding(
                source_id=source.id,
                artifact_kind=artifact_kind,
                adapter_type=adapter_type,
                adapter_base_path=adapter_base_path,
            )
            session.add(binding)
            await session.commit()
            await session.refresh(source)
            await session.refresh(binding)
            return source, binding

    async def list_sources(
        self, workspace_id: str
    ) -> list[tuple[CatalogSource, list[CatalogBinding]]]:
        async with self.async_session() as session:
            sources = list(
                (
                    await session.execute(
                        select(CatalogSource)
                        .where(CatalogSource.workspace_id == workspace_id)
                        .order_by(CatalogSource.display_name.asc(), CatalogSource.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            if not sources:
                return []
            bindings = list(
                (
                    await session.execute(
                        select(CatalogBinding).where(
                            CatalogBinding.source_id.in_([source.id for source in sources])
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_source: dict[object, list[CatalogBinding]] = {}
            for binding in bindings:
                by_source.setdefault(binding.source_id, []).append(binding)
            return [(source, by_source.get(source.id, [])) for source in sources]

    async def get_source_binding(
        self, workspace_id: str, source_id: str, artifact_kind: str = "mcp_server"
    ) -> tuple[CatalogSource, CatalogBinding] | None:
        async with self.async_session() as session:
            row = (
                await session.execute(
                    select(CatalogSource, CatalogBinding)
                    .join(CatalogBinding, CatalogBinding.source_id == CatalogSource.id)
                    .where(
                        CatalogSource.workspace_id == workspace_id,
                        CatalogSource.id == source_id,
                        CatalogBinding.artifact_kind == artifact_kind,
                    )
                )
            ).first()
            return (row[0], row[1]) if row else None

    async def update_source(
        self,
        workspace_id: str,
        source_id: str,
        changes: dict[str, object],
        *,
        clear_artifacts: bool = False,
    ) -> tuple[CatalogSource, CatalogBinding] | None:
        async with self.async_session() as session:
            row = (
                await session.execute(
                    select(CatalogSource, CatalogBinding)
                    .join(CatalogBinding, CatalogBinding.source_id == CatalogSource.id)
                    .where(
                        CatalogSource.workspace_id == workspace_id,
                        CatalogSource.id == source_id,
                        CatalogBinding.artifact_kind == "mcp_server",
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return None
            source, binding = row
            for field, value in changes.items():
                setattr(source, field, value)
            source.authority_generation = int(source.authority_generation or 1) + 1
            if clear_artifacts:
                await session.execute(
                    delete(CatalogArtifact).where(CatalogArtifact.binding_id == binding.id)
                )
                binding.sync_status = "pending"
                binding.last_sync_error = None
                binding.last_sync_at = None
                binding.sync_cursor = None
            await session.commit()
            await session.refresh(source)
            await session.refresh(binding)
            return source, binding

    async def clear_artifacts(self, binding_id: object) -> None:
        async with self.async_session() as session:
            await session.execute(
                delete(CatalogArtifact).where(CatalogArtifact.binding_id == binding_id)
            )
            await session.commit()

    async def delete_source(self, workspace_id: str, source_id: str) -> bool:
        async with self.async_session() as session:
            source = (
                (
                    await session.execute(
                        select(CatalogSource).where(
                            CatalogSource.workspace_id == workspace_id,
                            CatalogSource.id == source_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if source is None:
                return False
            await session.delete(source)
            await session.commit()
            return True

    async def delete_workspace_sources(self, workspace_id: str) -> int:
        """Delete all catalog state owned by one terminal workspace."""

        async with self.async_session() as session:
            source_ids = list(
                (
                    await session.execute(
                        select(CatalogSource.id).where(CatalogSource.workspace_id == workspace_id)
                    )
                ).scalars()
            )
            await session.execute(
                delete(CatalogArtifact).where(CatalogArtifact.workspace_id == workspace_id)
            )
            if source_ids:
                await session.execute(
                    delete(CatalogBinding).where(CatalogBinding.source_id.in_(source_ids))
                )
            result = await session.execute(
                delete(CatalogSource).where(CatalogSource.workspace_id == workspace_id)
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def count_workspace_sources(self, workspace_id: str) -> int:
        async with self.async_session() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CatalogSource)
                        .where(CatalogSource.workspace_id == workspace_id)
                    )
                ).scalar_one()
            )

    async def mark_sync_started(self, binding_id: object) -> None:
        async with self.async_session() as session:
            binding = await session.get(CatalogBinding, binding_id)
            if binding:
                binding.sync_status = "syncing"
                binding.last_sync_started_at = datetime.now(UTC)
                binding.last_sync_error = None
                await session.commit()

    async def mark_sync_finished(self, binding_id: object, *, error: str | None = None) -> None:
        async with self.async_session() as session:
            binding = await session.get(CatalogBinding, binding_id)
            if binding:
                binding.sync_status = "error" if error else "ready"
                binding.last_sync_error = error
                if not error:
                    binding.last_sync_at = datetime.now(UTC)
                    binding.sync_cursor = None
                await session.commit()

    async def upsert_artifacts(
        self,
        *,
        workspace_id: str,
        source_id: object,
        binding_id: object,
        artifacts: Iterable[NormalizedMcpArtifact],
    ) -> int:
        async with self.async_session() as session:
            count = await self._upsert_artifacts_in_session(
                session,
                workspace_id=workspace_id,
                source_id=source_id,
                binding_id=binding_id,
                artifacts=artifacts,
            )
            await session.commit()
        return count

    @staticmethod
    async def _upsert_artifacts_in_session(
        session,
        *,
        workspace_id: str,
        source_id: object,
        binding_id: object,
        artifacts: Iterable[NormalizedMcpArtifact],
    ) -> int:
        count = 0
        for artifact in artifacts:
            existing = (
                (
                    await session.execute(
                        select(CatalogArtifact).where(
                            CatalogArtifact.binding_id == binding_id,
                            CatalogArtifact.artifact_name == artifact.name,
                            CatalogArtifact.version == artifact.version,
                        )
                    )
                )
                .scalars()
                .first()
            )
            values = {
                "title": artifact.title,
                "description": artifact.description,
                "digest": artifact.digest,
                "metadata_json": artifact.metadata,
                "payload_json": artifact.payload,
                "compatible": artifact.compatible,
                "incompatibility_reason": artifact.incompatibility_reason,
                "remote_endpoints": artifact.remote_endpoints,
                "published_at": artifact.published_at,
                "upstream_updated_at": artifact.updated_at,
            }
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
            else:
                session.add(
                    CatalogArtifact(
                        workspace_id=workspace_id,
                        source_id=source_id,
                        binding_id=binding_id,
                        artifact_kind="mcp_server",
                        artifact_name=artifact.name,
                        version=artifact.version,
                        **values,
                    )
                )
            count += 1
        return count

    @staticmethod
    def _sync_configuration_snapshot(
        source: CatalogSource,
        binding: CatalogBinding,
    ) -> tuple[object, ...]:
        return (
            str(source.id),
            source.workspace_id,
            int(getattr(source, "authority_generation", 1) or 1),
            source.base_url,
            source.auth_type,
            source.auth_secret_name,
            source.auth_header_name,
            source.network_route,
            bool(source.enabled),
            bool(getattr(source, "credential_transitioning", False)),
            str(binding.id),
            binding.adapter_type,
            binding.adapter_base_path,
        )

    @classmethod
    def _sync_authority_snapshot(
        cls,
        source: CatalogSource,
        binding: CatalogBinding,
    ) -> tuple[object, ...]:
        return (
            *cls._sync_configuration_snapshot(source, binding),
            int(getattr(binding, "sync_generation", 1) or 1),
        )

    async def _begin_sync_if_current(
        self,
        source: CatalogSource,
        binding: CatalogBinding,
    ) -> tuple[CatalogSource, CatalogBinding]:
        expected = self._sync_configuration_snapshot(source, binding)
        async with self.async_session() as session:
            row = (
                await session.execute(
                    select(CatalogSource, CatalogBinding)
                    .join(CatalogBinding, CatalogBinding.source_id == CatalogSource.id)
                    .where(
                        CatalogSource.workspace_id == source.workspace_id,
                        CatalogSource.id == source.id,
                        CatalogBinding.id == binding.id,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                raise ValueError("Catalog source changed while synchronization was prepared")
            current_source, current_binding = row
            if self._sync_configuration_snapshot(current_source, current_binding) != expected:
                raise ValueError("Catalog source changed while synchronization was prepared")
            current_binding.sync_generation = (
                int(getattr(current_binding, "sync_generation", 1) or 1) + 1
            )
            current_binding.sync_status = "syncing"
            current_binding.last_sync_started_at = datetime.now(UTC)
            current_binding.last_sync_error = None
            await session.commit()
            await session.refresh(current_source)
            await session.refresh(current_binding)
            return current_source, current_binding

    async def _finish_sync_if_current(
        self,
        source: CatalogSource,
        binding: CatalogBinding,
        *,
        artifacts: list[NormalizedMcpArtifact] | None,
        incremental: bool,
        error: str | None,
    ) -> int:
        # Serialize only the bounded artifact commit with source mutations and
        # terminal workspace cleanup. Remote pagination remains outside this
        # broad lifecycle lock.
        async with mcp_lifecycle_store.workspace_mutation_lock(source.workspace_id):
            return await self._finish_sync_if_current_locked(
                source,
                binding,
                artifacts=artifacts,
                incremental=incremental,
                error=error,
            )

    async def _finish_sync_if_current_locked(
        self,
        source: CatalogSource,
        binding: CatalogBinding,
        *,
        artifacts: list[NormalizedMcpArtifact] | None,
        incremental: bool,
        error: str | None,
    ) -> int:
        expected = self._sync_authority_snapshot(source, binding)
        async with self.async_session() as session:
            row = (
                await session.execute(
                    select(CatalogSource, CatalogBinding)
                    .join(CatalogBinding, CatalogBinding.source_id == CatalogSource.id)
                    .where(
                        CatalogSource.workspace_id == source.workspace_id,
                        CatalogSource.id == source.id,
                        CatalogBinding.id == binding.id,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                raise ValueError("Catalog source changed while synchronization was in flight")
            current_source, current_binding = row
            if self._sync_authority_snapshot(current_source, current_binding) != expected:
                raise ValueError("Catalog source changed while synchronization was in flight")
            if error is not None:
                current_binding.sync_status = "error"
                current_binding.last_sync_error = error
                await session.commit()
                return 0
            if not incremental:
                await session.execute(
                    delete(CatalogArtifact).where(CatalogArtifact.binding_id == current_binding.id)
                )
            count = await self._upsert_artifacts_in_session(
                session,
                workspace_id=current_source.workspace_id,
                source_id=current_source.id,
                binding_id=current_binding.id,
                artifacts=artifacts or [],
            )
            current_binding.sync_status = "ready"
            current_binding.last_sync_error = None
            current_binding.last_sync_at = datetime.now(UTC)
            current_binding.sync_cursor = None
            await session.commit()
            return count

    async def sync_mcp_binding(
        self,
        source: CatalogSource,
        binding: CatalogBinding,
        *,
        headers: dict[str, str] | None = None,
        incremental: bool = True,
    ) -> int:
        if source.network_route == "connector":
            raise ValueError("Connector-routed catalog access is not available in this deployment")
        if binding.adapter_type != "mcp_registry_v0_1":
            raise ValueError("Catalog adapter is not allowlisted")
        source, binding = await self._begin_sync_if_current(source, binding)
        adapter = McpRegistryV01Adapter(
            source.base_url,
            base_path=binding.adapter_base_path,
            headers=headers,
        )
        normalized_artifacts: list[NormalizedMcpArtifact] = []
        cursor: str | None = None
        updated_since = binding.last_sync_at if incremental else None
        try:
            for _page_number in range(settings.CATALOG_MAX_SYNC_PAGES):
                page = await adapter.list_updated(cursor=cursor, updated_since=updated_since)
                for item in page.items:
                    try:
                        from app.catalog.adapter import normalize_mcp_registry_entry

                        normalized_artifacts.append(normalize_mcp_registry_entry(item))
                    except ValueError:
                        logger.warning(
                            "catalog_artifact_rejected",
                            workspace_id=source.workspace_id,
                            source_id=str(source.id),
                            adapter_type=binding.adapter_type,
                            error_code="MALFORMED_REGISTRY_ENTRY",
                        )
                        continue
                cursor = page.next_cursor
                if not cursor:
                    break
            else:
                raise ValueError("Registry pagination exceeded the configured page limit")
        except Exception:
            with suppress(ValueError):
                await self._finish_sync_if_current(
                    source,
                    binding,
                    artifacts=None,
                    incremental=incremental,
                    error="Catalog synchronization failed",
                )
            raise
        count = await self._finish_sync_if_current(
            source,
            binding,
            artifacts=normalized_artifacts,
            incremental=incremental,
            error=None,
        )
        logger.info(
            "catalog_sync_completed",
            workspace_id=source.workspace_id,
            source_id=str(source.id),
            binding_id=str(binding.id),
            adapter_type=binding.adapter_type,
            artifact_count=count,
            incremental=bool(updated_since),
        )
        return count

    async def list_artifacts(
        self,
        workspace_id: str,
        *,
        artifact_kind: str = "mcp_server",
        source_id: str | None = None,
        search: str | None = None,
        compatible: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CatalogArtifact]:
        async with self.async_session() as session:
            stmt = (
                select(CatalogArtifact)
                .join(CatalogSource, CatalogSource.id == CatalogArtifact.source_id)
                .where(
                    CatalogArtifact.workspace_id == workspace_id,
                    CatalogArtifact.artifact_kind == artifact_kind,
                    CatalogSource.enabled.is_(True),
                )
            )
            if source_id:
                stmt = stmt.where(CatalogArtifact.source_id == source_id)
            if compatible is not None:
                stmt = stmt.where(CatalogArtifact.compatible == compatible)
            if search:
                pattern = f"%{search.strip()}%"
                stmt = stmt.where(
                    or_(
                        CatalogArtifact.artifact_name.ilike(pattern),
                        CatalogArtifact.title.ilike(pattern),
                        CatalogArtifact.description.ilike(pattern),
                    )
                )
            stmt = (
                stmt.order_by(
                    CatalogArtifact.artifact_name.asc(),
                    CatalogArtifact.version.desc(),
                    CatalogArtifact.id.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def get_artifact(
        self,
        workspace_id: str,
        *,
        artifact_id: str | None = None,
        source_id: str | None = None,
        artifact_name: str | None = None,
        version: str | None = None,
    ) -> CatalogArtifact | None:
        async with self.async_session() as session:
            stmt = (
                select(CatalogArtifact)
                .join(CatalogSource, CatalogSource.id == CatalogArtifact.source_id)
                .where(
                    CatalogArtifact.workspace_id == workspace_id,
                    CatalogSource.enabled.is_(True),
                )
            )
            if artifact_id:
                stmt = stmt.where(CatalogArtifact.id == artifact_id)
            else:
                stmt = stmt.where(
                    CatalogArtifact.source_id == source_id,
                    CatalogArtifact.artifact_name == artifact_name,
                )
                if version:
                    stmt = stmt.where(CatalogArtifact.version == version)
            stmt = stmt.order_by(CatalogArtifact.upstream_updated_at.desc().nullslast())
            return (await session.execute(stmt)).scalars().first()


catalog_store = CatalogStore(settings.DATABASE_URL)
