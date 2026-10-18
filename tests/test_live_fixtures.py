import json
from pathlib import Path

import jsonschema
import pytest

from meta_search.config import load_settings
from meta_search.outputs import OUTPUTS
from meta_search.pipelines.collect_page import validate_snapshot
from meta_search.pipelines.experience import validate_units

FIXTURES = Path(__file__).with_name("fixtures")


@pytest.mark.parametrize(
    "fixture,schema",
    [
        ("runtime_page_snapshot.json", "page_snapshot.schema.json"),
        ("runtime_codex.json", "opinion_units.schema.json"),
        ("runtime_agy.json", "opinion_units.schema.json"),
    ],
)
def test_real_cli_output_fixtures(fixture, schema):
    output = json.loads((FIXTURES / fixture).read_text())
    settings = load_settings()
    spec = json.loads((settings.schemas_dir / schema).read_text())
    jsonschema.validate(output, spec)
    OUTPUTS[schema].model_validate(output)
    if schema == "page_snapshot.schema.json":
        cleaned = validate_snapshot(output, 100)
        assert cleaned["page_ok"] and cleaned["reviews"]
    else:
        units, dropped = validate_units(output, "타건감이 좋고 조용해서 만족합니다.")
        assert units and dropped == 0
