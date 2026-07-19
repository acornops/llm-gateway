import uuid

from sqlalchemy import (
    JSON,
    UUID,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.secrets.db_models import Base


class CatalogSource(Base):
    __tablename__ = "gateway_catalog_sources"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "display_name", name="uq_gateway_catalog_source_name"
        ),
        Index("ix_gateway_catalog_sources_workspace", "workspace_id", "enabled"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    base_url = Column(String, nullable=False)
    auth_type = Column(String, nullable=False, default="none")
    auth_secret_name = Column(String, nullable=True)
    auth_header_name = Column(String, nullable=True)
    network_route = Column(String, nullable=False, default="direct")
    enabled = Column(Boolean, nullable=False, default=True)
    management_mode = Column(String, nullable=False, default="workspace")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class CatalogBinding(Base):
    __tablename__ = "gateway_catalog_bindings"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "artifact_kind", name="uq_gateway_catalog_binding_kind"
        ),
        Index("ix_gateway_catalog_bindings_source", "source_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id = Column(
        UUID(as_uuid=True),
        ForeignKey("gateway_catalog_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_kind = Column(String, nullable=False)
    adapter_type = Column(String, nullable=False)
    adapter_base_path = Column(String, nullable=False, default="/v0.1")
    sync_status = Column(String, nullable=False, default="pending")
    last_sync_started_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_error = Column(String, nullable=True)
    sync_cursor = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class CatalogArtifact(Base):
    __tablename__ = "gateway_catalog_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "binding_id",
            "artifact_name",
            "version",
            name="uq_gateway_catalog_artifact_version",
        ),
        Index(
            "ix_gateway_catalog_artifacts_workspace_kind",
            "workspace_id",
            "artifact_kind",
            "artifact_name",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(String, nullable=False)
    source_id = Column(
        UUID(as_uuid=True),
        ForeignKey("gateway_catalog_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    binding_id = Column(
        UUID(as_uuid=True),
        ForeignKey("gateway_catalog_bindings.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_kind = Column(String, nullable=False)
    artifact_name = Column(String, nullable=False)
    title = Column(String, nullable=True)
    description = Column(String, nullable=False)
    version = Column(String, nullable=False)
    digest = Column(String, nullable=False)
    metadata_json = Column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    payload_json = Column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    compatible = Column(Boolean, nullable=False, default=False)
    incompatibility_reason = Column(String, nullable=True)
    remote_endpoints = Column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )
    published_at = Column(DateTime(timezone=True), nullable=True)
    upstream_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
