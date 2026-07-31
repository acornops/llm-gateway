"""Integrity checks for sanitized provider stream replay fixtures."""

import json
import re
from pathlib import Path

from provider_replay_fixtures import FIXTURE_ROOT, load_replay_cases

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = FIXTURE_ROOT / "provider_stream_replay_manifest.json"


def test_provider_stream_replay_manifest_covers_all_surfaces_and_locked_sdks() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())
    surfaces = manifest["surfaces"]

    assert manifest["schema_version"] == 1
    assert {surface["id"] for surface in surfaces} == {
        "anthropic-messages",
        "gemini-generate-content",
        "openai-chat-completions",
        "openai-responses",
    }
    requirements = (ROOT / "requirements.lock").read_text()
    for surface in surfaces:
        cases = load_replay_cases(surface["fixture"])
        assert len(cases) >= surface["minimum_cases"]
        assert len({case["name"] for case in cases}) == len(cases)
        locked = rf"^{re.escape(surface['sdk_package'])}=={re.escape(surface['sdk_version'])}\b"
        assert re.search(locked, requirements, flags=re.MULTILINE), (
            f"{surface['id']} replay fixtures must be reviewed when its SDK changes"
        )


def test_provider_stream_replays_contain_no_credential_shaped_data() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text())

    for surface in manifest["surfaces"]:
        fixture_text = (FIXTURE_ROOT / surface["fixture"]).read_text().lower()
        assert "api_key" not in fixture_text
        assert "authorization" not in fixture_text
        assert "bearer " not in fixture_text
        assert "sk-" not in fixture_text
