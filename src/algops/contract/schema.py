"""JSON Schemas shipped in the contract bundle (editor validation / coding agents).

``algops.schema.json`` is generated from the pydantic models (minus wire-only fields), so it can
never drift from what the runner actually accepts — tests/test_v2_contract.py enforces that the
shipped copy is current (regenerate with ``python -m algops.contract.schema``).
"""

from __future__ import annotations

import json
import os

from algops.core.manifest import RepoManifest

BUNDLE = os.path.join(os.path.dirname(__file__), "bundle")
_WIRE_ONLY = {"param_spec", "legacy"}


def _strip(node):
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            for k in _WIRE_ONLY:
                props.pop(k, None)
        return {k: _strip(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def manifest_json_schema() -> dict:
    schema = _strip(RepoManifest.model_json_schema())
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "ALGOps algops.yaml (contract v2)"
    return schema


def render() -> str:
    return json.dumps(manifest_json_schema(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    path = os.path.join(BUNDLE, "algops.schema.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render())
    print("wrote", path)
