import uuid

from sqlalchemy import (
    JSON,
    UUID,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.secrets.db_models import Base


class Tool(Base):
    __tablename__ = "gateway_tools"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "target_id", "tool_name",
            name="uq_gateway_tools_ws_target_name",
        ),
        Index("ix_gateway_tools_workspace_target", "workspace_id", "target_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(String, nullable=False)
    target_id = Column(String, nullable=False)
    target_type = Column(String, nullable=False)
    tool_name = Column(String, nullable=False)
    mcp_server_url = Column(String, nullable=False)
    enabled = Column(Boolean, default=True)
    input_schema = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    description = Column(String, nullable=True)
    capability = Column(String, nullable=False, default="write")
    version = Column(String, nullable=False, default="v1")
    source = Column(String, nullable=False, default="mcp")
    timeout_ms = Column(Integer, default=10000)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class McpServer(Base):
    __tablename__ = "gateway_mcp_servers"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "target_id", "server_name",
            name="uq_gateway_mcp_servers_ws_target_name",
        ),
        UniqueConstraint(
            "workspace_id", "target_id", "server_url",
            name="uq_gateway_mcp_servers_ws_target_url",
        ),
        Index("ix_gateway_mcp_servers_workspace_target", "workspace_id", "target_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(String, nullable=False)
    target_id = Column(String, nullable=False)
    target_type = Column(String, nullable=False)
    server_name = Column(String, nullable=False)
    server_url = Column(String, nullable=False)
    enabled = Column(Boolean, default=True)
    auth_type = Column(String, nullable=False, default="none")
    auth_secret_name = Column(String, nullable=True)
    auth_header_name = Column(String, nullable=True)
    auth_header_prefix = Column(String, nullable=True)
    public_headers = Column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    connection_status = Column(String, nullable=False, default="unknown")
    last_discovery_at = Column(DateTime(timezone=True), nullable=True)
    last_discovery_error = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
