from __future__ import annotations

import json
from pathlib import Path

from forge_core.engine import AnalysisEngine, PluginRegistration
from forge_core.models import EngineeringSpec
from plugins.fake import FakePhysicsPlugin


def main() -> None:
    example_path = Path(__file__).parents[1] / "examples" / "fake_spec.json"
    spec = EngineeringSpec.model_validate_json(example_path.read_text(encoding="utf-8"))
    plugin = FakePhysicsPlugin()
    engine = AnalysisEngine(
        engine_version="0.1.0",
        policy_version="1.0.0",
        allowed_plugins={
            PluginRegistration(
                plugin_id="fake.double",
                plugin_version="1.0.0",
                schema_version="1.0.0",
                artifact_hash=(
                    "sha256:6276d94a0dba2fd8c46f43cd809fd47b"
                    "e80eef8eb62a2318b086b69b3b269cda"
                ),
                artifact_path=Path(__file__).parents[1] / "plugins" / "fake.py",
            )
        },
    )
    outcome = engine.execute(spec, plugin)
    print(json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
