import uuid

from sqlalchemy import (
    JSON,
    UUID,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func, text

from app.secrets.db_models import Base


class Tool(Base):
    __tablename__ = "gateway_tools"
    __table_args__ = (
        UniqueConstraint(
            "server_id",
            "tool_name",
            name="uq_gateway_tools_ws_target_name",
        ),
        Index("ix_gateway_tools_workspace_target", "workspace_id", "target_id"),
        Index("ix_gateway_tools_workspace_agent", "workspace_id", "agent_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    server_id = Column(
        UUID(as_uuid=True),
        ForeignKey("gateway_mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id = Column(String, nullable=False)
    scope_type = Column(String, nullable=False, default="target")
    agent_id = Column(String, nullable=True)
    target_id = Column(String, nullable=True)
    target_type = Column(String, nullable=True)
    tool_name = Column(String, nullable=False)
    mcp_server_url = Column(String, nullable=False)
    enabled = Column(Boolean, default=True)
    input_schema = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    output_schema = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    artifact_policy = Column(String, nullable=False, default="never")
    description = Column(String, nullable=True)
    capability = Column(String, nullable=False, default="write")
    review_state = Column(String, nullable=False, default="pending")
    risk_level = Column(String, nullable=False, default="high_risk")
    auto_allowed = Column(Boolean, nullable=False, default=False)
    version = Column(String, nullable=False, default="v1")
    source = Column(String, nullable=False, default="mcp")
    timeout_ms = Column(Integer, default=10000)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class McpServer(Base):
    __tablename__ = "gateway_mcp_servers"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "scope_type",
            "target_id",
            "server_name",
            name="uq_gateway_mcp_servers_ws_target_name",
        ),
        UniqueConstraint(
            "workspace_id",
            "scope_type",
            "agent_id",
            "server_name",
            name="uq_gateway_mcp_servers_ws_agent_name",
        ),
        UniqueConstraint(
            "workspace_id",
            "scope_type",
            "target_id",
            "server_url",
            name="uq_gateway_mcp_servers_ws_target_url",
        ),
        UniqueConstraint(
            "workspace_id",
            "scope_type",
            "agent_id",
            "server_url",
            name="uq_gateway_mcp_servers_ws_agent_url",
        ),
        UniqueConstraint("id", "workspace_id", name="uq_gateway_mcp_servers_id_workspace"),
        Index("ix_gateway_mcp_servers_workspace_target", "workspace_id", "target_id"),
        Index("ix_gateway_mcp_servers_workspace_agent", "workspace_id", "agent_id"),
        Index(
            "uq_gateway_mcp_servers_builtin_destination",
            "workspace_id",
            "scope_type",
            "target_id",
            "target_type",
            unique=True,
            postgresql_where=text("provenance_type='builtin'"),
            sqlite_where=text("provenance_type='builtin'"),
        ),
        CheckConstraint(
            "provenance_type IN ('manual','catalog','builtin')",
            name="ck_gateway_mcp_servers_provenance_type",
        ),
        CheckConstraint(
            "credential_mode IN ('none','workspace','individual')",
            name="ck_gateway_mcp_servers_credential_mode",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(String, nullable=False)
    scope_type = Column(String, nullable=False, default="target")
    agent_id = Column(String, nullable=True)
    target_id = Column(String, nullable=True)
    target_type = Column(String, nullable=True)
    server_name = Column(String, nullable=False)
    server_url = Column(String, nullable=False)
    enabled = Column(Boolean, default=True)
    auth_type = Column(String, nullable=False, default="none")
    auth_header_name = Column(String, nullable=True)
    auth_header_prefix = Column(String, nullable=True)
    public_headers = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    credential_mode = Column(String, nullable=False, default="none")
    credential_transitioning = Column(Boolean, nullable=False, default=False)
    catalog_source_id = Column(UUID(as_uuid=True), nullable=True)
    catalog_artifact_name = Column(String, nullable=True)
    catalog_version = Column(String, nullable=True)
    catalog_digest = Column(String, nullable=True)
    catalog_imported_at = Column(DateTime(timezone=True), nullable=True)
    provenance_type = Column(String, nullable=False, default="manual")
    endpoint_configuration = Column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    revision = Column(Integer, nullable=False, default=1)
    connection_status = Column(String, nullable=False, default="unknown")
    last_discovery_at = Column(DateTime(timezone=True), nullable=True)
    last_discovery_error = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class ApprovalReceiptUse(Base):
    __tablename__ = "gateway_approval_receipt_uses"

    jti = Column(String, primary_key=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    claimed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class McpConnection(Base):
    __tablename__ = "gateway_mcp_connections"
    __table_args__ = (
        UniqueConstraint(
            "server_id", "owner_type", "owner_id", name="uq_gateway_mcp_connection_owner"
        ),
        Index(
            "ix_gateway_mcp_connections_workspace_owner",
            "workspace_id",
            "owner_type",
            "owner_id",
        ),
        ForeignKeyConstraint(
            ["server_id", "workspace_id"],
            ["gateway_mcp_servers.id", "gateway_mcp_servers.workspace_id"],
            name="fk_gateway_mcp_connection_server_workspace",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending_authorization', 'connected', 'reauthorization_required', 'error')",
            name="ck_gateway_mcp_connection_status",
        ),
        CheckConstraint(
            "owner_type IN ('installation', 'user')",
            name="ck_gateway_mcp_connection_owner_type",
        ),
        CheckConstraint(
            "(owner_type = 'installation' AND owner_id = 'installation') OR "
            "(owner_type = 'user' AND length(owner_id) > 0)",
            name="ck_gateway_mcp_connection_owner_id",
        ),
        CheckConstraint(
            "error_code IS NULL OR length(error_code) <= 64",
            name="ck_gateway_mcp_connection_error_code",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(String, nullable=False)
    server_id = Column(
        UUID(as_uuid=True),
        nullable=False,
    )
    owner_type = Column(String, nullable=False)
    owner_id = Column(String, nullable=False)
    status = Column(String, nullable=False, default="error")
    verified_tool_names = Column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list
    )
    verified_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    oauth_issuer = Column(String, nullable=True)
    oauth_registration_method = Column(String, nullable=True)
    oauth_resource = Column(String, nullable=True)
    oauth_client_id = Column(String, nullable=True)
    oauth_endpoint_snapshot = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    oauth_scopes = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list)
    oauth_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    oauth_refresh_capable = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class McpOAuthRegistration(Base):
    """Non-secret public OAuth client registration pinned to one issuer."""

    __tablename__ = "gateway_mcp_oauth_registrations"
    __table_args__ = (
        UniqueConstraint(
            "server_id",
            "issuer",
            name="uq_gateway_mcp_oauth_registration_server_issuer",
        ),
        Index(
            "ix_gateway_mcp_oauth_registrations_workspace_server",
            "workspace_id",
            "server_id",
        ),
        ForeignKeyConstraint(
            ["server_id", "workspace_id"],
            ["gateway_mcp_servers.id", "gateway_mcp_servers.workspace_id"],
            name="fk_gateway_mcp_oauth_registration_server_workspace",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "registration_method IN ('cimd', 'dcr')",
            name="ck_gateway_mcp_oauth_registration_method",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(String, nullable=False)
    server_id = Column(UUID(as_uuid=True), nullable=False)
    resource = Column(String, nullable=False)
    issuer = Column(String, nullable=False)
    registration_method = Column(String, nullable=False)
    client_id = Column(String, nullable=False)
    endpoint_snapshot = Column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )
    metadata_fingerprint = Column(String(64), nullable=False)
    client_metadata_fingerprint = Column(String(64), nullable=False)
    scopes = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
