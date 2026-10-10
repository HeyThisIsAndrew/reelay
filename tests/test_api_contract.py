"""api/openapi.yaml must match the live /openapi.json exactly.

If this fails after an intentional API change, regenerate the contract:
    python -m reelay.api --write-contract
"""

from __future__ import annotations

import json

import requests
import yaml

from reelay.api import CONTRACT_PATH, ApiServer, create_app
from tests.fakes import make_world


def _diff(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"{path}/{key}: only in YAML")
            elif key not in b:
                out.append(f"{path}/{key}: only in live spec")
            else:
                out += _diff(a[key], b[key], f"{path}/{key}")
        return out
    return [] if a == b else [f"{path}: live={json.dumps(a)[:120]} yaml={json.dumps(b)[:120]}"]


def test_live_openapi_matches_yaml_contract(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    server = ApiServer(create_app(world.store, world.pipeline, world.accounts, lambda: "t"), host="127.0.0.1", port=0)
    assert server.start()
    try:
        live = requests.get(server.url + "/openapi.json", timeout=10).json()
    finally:
        server.stop()
        world.store.close()
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    differences = _diff(live, contract)
    assert not differences, ("api/openapi.yaml is out of date. Run `python -m reelay.api --write-contract`.\n"
                             + "\n".join(differences[:20]))


def test_contract_covers_every_endpoint_with_auth_and_errors():
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["openapi"].startswith("3.1")
    expected = {("/v1/board", "get"): {"200", "400", "401", "422"},
                ("/v1/videos/{video_id}", "patch"): {"200", "401", "404", "409", "422"},
                ("/v1/videos/{video_id}/schedule", "post"): {"202", "401", "404", "409", "422", "429"},
                ("/v1/platforms", "get"): {"200", "400", "401", "422"},
                ("/v1/sources", "get"): {"200", "400", "401", "422"},
                ("/v1/sources", "post"): {"201", "400", "401", "422"},
                ("/v1/videos/{video_id}/revisions", "get"): {"200", "401", "404", "422"},
                ("/v1/videos/{video_id}/manual-posts", "post"): {"202", "401", "404", "422", "429"},
                ("/v1/sources/{source_id}", "patch"): {"200", "401", "404", "422"},
                ("/v1/sources/{source_id}", "delete"): {"202", "401", "404", "422", "429"},
                ("/v1/videos/bulk-update", "post"): {"202", "401", "404", "409", "422", "429"},
                ("/v1/videos/fill-from-original", "post"): {"200", "401", "404", "422"},
                ("/v1/videos/{video_id}/archive", "post"): {"202", "401", "404", "409", "422", "429"},
                ("/v1/videos/{video_id}/unarchive", "post"): {"202", "401", "404", "422", "429"},
                ("/v1/videos/{video_id}/revisions/{revision_id}/restore", "post"): {"200", "401", "404", "409", "422"},
                ("/v1/videos/{video_id}/transcript", "post"): {"200", "401", "404", "422"},
                ("/v1/import", "post"): {"200", "202", "400", "401", "422", "429"},
                ("/v1/approvals", "get"): {"200", "400", "401", "422"},
                ("/v1/approvals/{approval_id}", "get"): {"200", "401", "404", "422"}}
    for (path, method), statuses in expected.items():
        op = contract["paths"][path][method]
        assert set(op["responses"]) == statuses, (path, method)
        assert op["security"] == [{"HTTPBearer": []}] and op["operationId"]
        for status in statuses - {"200", "201", "202", "204"}:
            ref = op["responses"][status]["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith("/ErrorBody")
    assert not any(p.startswith("/media") for p in contract["paths"])  # expiring links aren't agent tools
    patch = contract["components"]["schemas"]["VideoPatch"]
    assert patch["additionalProperties"] is False
    assert set(patch["properties"]) == {"title", "description", "tags", "target_platforms", "tiktok_text",
                                        "instagram_text", "bluesky_text", "threads_text", "facebook_text",
                                        "hashtags", "transcript", "thumbnail_path", "content_type", "topic",
                                        "keywords", "creator", "original_publish_date", "source_platform",
                                        "source_url", "authorization", "derived_from"}   # never imported_at


def test_contract_documents_the_ready_gate():
    """Agents must be able to handle the Ready gate from the contract alone."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    schedule = contract["paths"]["/v1/videos/{video_id}/schedule"]["post"]
    force = next(p for p in schedule["parameters"] if p["name"] == "force")
    assert force["in"] == "query" and force["schema"]["type"] == "boolean" and force["schema"]["default"] is False
    assert "ticket_not_ready" in schedule["responses"]["422"]["description"]
    assert "force=true" in schedule["responses"]["422"]["description"]
    video = contract["components"]["schemas"]["Video"]
    assert {"ready", "readiness", "ticket", "provenance", "publishing_history"} <= set(video["required"])
    states = contract["components"]["schemas"]["PlatformReadinessOut"]["properties"]["state"]["enum"]
    assert states == ["ready", "not_ready", "not_checked"]
