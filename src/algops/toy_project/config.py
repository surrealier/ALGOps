"""Generated config loader for the toy project (plan §2A pattern).

pydantic-settings does NOT read YAML by default, so we must register the
``YamlConfigSettingsSource`` explicitly in ``settings_customise_sources``. ``load_param_spec``
also parses the same file into the ALGOps ``ParamSpec`` so the master can introspect ranges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from algops.core import ParamSpec

_YAML = Path(__file__).with_name("params.yaml")


class _FlatYamlSource(PydanticBaseSettingsSource):
    """Read params.yaml as UTF-8 and flatten ``params: [{key, default}]`` -> {flat_key: default}.

    We implement the source directly rather than subclass ``YamlConfigSettingsSource`` because
    that class reads the file with the platform default codec in its ``__init__`` — which fails
    on UTF-8 content (e.g. Korean/'→') under a cp949 Windows locale.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        raw = yaml.safe_load(_YAML.read_text(encoding="utf-8")) or {}
        out: dict[str, Any] = {}
        for p in raw.get("params", []):
            out[p["key"].replace(".", "_")] = p["default"]
        return out


class ToyParams(BaseSettings):
    """Typed view of the toy project's externalized parameters."""

    model_config = SettingsConfigDict(extra="ignore")

    detector_confidence_threshold: float = 0.45
    postproc_roundness_min: float = 0.0

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, _FlatYamlSource(settings_cls))


def load_params() -> ToyParams:
    return ToyParams()


def load_param_spec() -> ParamSpec:
    raw = yaml.safe_load(_YAML.read_text(encoding="utf-8")) or {}
    return ParamSpec(
        version=int(raw.get("meta", {}).get("param_spec_version", 1)),
        modality=str(raw.get("meta", {}).get("modality", "tabular")),
        params=raw.get("params", []),
    )
