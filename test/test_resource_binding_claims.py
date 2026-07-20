import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.auth.claims import Permissions
from app.mcp.canonical_json import canonical_json

VECTOR = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "contracts/resource-binding-digest-conformance.json"
    ).read_text()
)


def binding():
    value = VECTOR["bindings"][0]
    return {
        "binding_id": value["bindingId"],
        "type": value["type"],
        "resource_id": value["resourceId"],
        "provider": value["provider"],
        "provider_version": value["providerVersion"],
        "workspace_id": value["workspaceId"],
        "label_snapshot": value["labelSnapshot"],
        "source": value["source"],
        "operations": value["operations"],
        "context_mode": value["contextMode"],
        "provider_data": value["providerData"],
    }


def test_generic_resource_binding_claim_digest_is_verified():
    value = binding()
    permissions = Permissions(resource_bindings=[value], binding_digest=VECTOR["sha256"])
    assert permissions.resource_bindings[0].resource_id == "artifact-1"


def test_resource_binding_claim_rejects_tampering_and_duplicates():
    value = binding()
    with pytest.raises(ValidationError, match="does not match"):
        Permissions(resource_bindings=[value], binding_digest="0" * 64)
    with pytest.raises(ValidationError, match="unique"):
        Permissions(resource_bindings=[value, value], binding_digest=VECTOR["sha256"])


def test_resource_binding_claim_rejects_ambiguous_operations():
    for operations in ([], ["read", "read"], [""]):
        value = {**binding(), "operations": operations}
        with pytest.raises(ValidationError, match="operations"):
            Permissions(resource_bindings=[value], binding_digest=VECTOR["sha256"])


def test_empty_resource_binding_digest_is_still_verified():
    empty_digest = hashlib.sha256(canonical_json([]).encode("utf-8")).hexdigest()
    Permissions(resource_bindings=[], binding_digest=empty_digest)
    with pytest.raises(ValidationError, match="does not match"):
        Permissions(resource_bindings=[], binding_digest="0" * 64)
