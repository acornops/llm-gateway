import uuid

from sqlalchemy import JSON, UUID, Column, DateTime, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()


class Secret(Base):
    __tablename__ = "gateway_secrets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_scope = Column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    secret_name = Column(String, nullable=False)
    ciphertext = Column(LargeBinary, nullable=False)
    nonce = Column(LargeBinary, nullable=False)
    aad = Column(LargeBinary, nullable=False)
    key_id = Column(String, nullable=False)
    version = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
