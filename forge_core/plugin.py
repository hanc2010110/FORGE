from __future__ import annotations

from typing import Protocol

from forge_core.models import (
    AnalysisResult,
    EngineeringSpec,
    PreflightResult,
    VerificationResult,
)


class PhysicsPlugin(Protocol):
    plugin_id: str
    plugin_version: str
    schema_version: str

    def preflight(self, spec: EngineeringSpec) -> PreflightResult: ...

    def run(self, spec: EngineeringSpec) -> AnalysisResult: ...

    def verify(
        self, spec: EngineeringSpec, result: AnalysisResult
    ) -> tuple[VerificationResult, ...]: ...
