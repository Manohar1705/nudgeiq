"""Load a use-case config from JSON and validate it."""

import json
from pathlib import Path

from app.models import TemplateLibrary, UseCaseConfig

USE_CASE_DIR = Path(__file__).parent / "use_cases"


def load_use_case(use_case_id: str, directory: Path | None = None) -> UseCaseConfig:
    path = (directory or USE_CASE_DIR) / f"{use_case_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No use case config at {path}")
    return UseCaseConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def list_use_cases(directory: Path | None = None) -> list[str]:
    return sorted(p.stem for p in (directory or USE_CASE_DIR).glob("*.json"))


def load_template_library(use_case_id: str, directory: Path | None = None) -> TemplateLibrary:
    base = directory or (Path(__file__).parent / "templates")
    path = base / f"{use_case_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No template library at {path}")
    return TemplateLibrary.model_validate(json.loads(path.read_text(encoding="utf-8")))
