from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from forge_core.conversational_design import ClaimConfidence, EvidenceClaimKind
from forge_core.hashing import canonical_sha256
from forge_core.models import ContractModel

_RELEASE_AUTHORITY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bready\b",
        r"\bblocked\b",
        r"\brelease\s+approved\b",
        r"\bsafe\s+for\s+production\b",
        r"출시\s*(?:불)?가능",
        r"배포\s*(?:불)?가능",
        r"승인\s*(?:완료|불?가능)",
        r"릴리스\s*(?:승인|불?가능)",
    )
)


def contains_release_authority_phrase(text: str) -> bool:
    return any(pattern.search(text) for pattern in _RELEASE_AUTHORITY_PATTERNS)


def redact_release_authority_phrases(text: str) -> str:
    redacted = text
    for pattern in _RELEASE_AUTHORITY_PATTERNS:
        redacted = pattern.sub("[release-state omitted]", redacted)
    return redacted


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class RuntimeDisposition(StrEnum):
    ANSWER = "answer"
    QUESTION = "question"
    UNKNOWN = "unknown"


class PromptManifest(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    prompt_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    system_contract: str = Field(min_length=1)
    safety_rules: tuple[str, ...] = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hash_must_match_payload(self) -> PromptManifest:
        expected = prompt_manifest_hash(
            self.prompt_id,
            self.prompt_version,
            self.system_contract,
            self.safety_rules,
        )
        if self.manifest_hash != expected:
            raise ValueError("prompt manifest hash does not match payload")
        return self


class ProviderManifest(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    temperature: float = Field(ge=0, le=1)
    tool_allowlist: tuple[str, ...] = ()
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hash_must_match_payload(self) -> ProviderManifest:
        expected = provider_manifest_hash(
            self.provider_id,
            self.model_id,
            self.model_version,
            self.temperature,
            self.tool_allowlist,
        )
        if self.manifest_hash != expected:
            raise ValueError("provider manifest hash does not match payload")
        return self


class ConversationRequest(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    request_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    user_message: str = Field(min_length=1)
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "requested_at")


class RetrievedContextChunk(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    context_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    source_kind: Literal[
        "cad",
        "bom",
        "firmware",
        "protocol",
        "test",
        "document",
        "simulation",
        "device",
        "user",
    ]
    source_uri: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_captured_at: datetime
    text: str = Field(min_length=1)
    source_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    chunk_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("source_captured_at")
    @classmethod
    def source_captured_at_must_be_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "source_captured_at")

    @model_validator(mode="after")
    def hash_must_match_payload(self) -> RetrievedContextChunk:
        expected = context_chunk_hash(
            self.context_id,
            self.tenant_id,
            self.project_id,
            self.source_kind,
            self.source_uri,
            self.source_version,
            self.source_captured_at,
            self.text,
            self.source_hash,
        )
        if self.chunk_hash != expected:
            raise ValueError("context chunk hash does not match payload")
        return self


class CitedClaim(ContractModel):
    claim_id: str = Field(min_length=1)
    kind: EvidenceClaimKind
    statement: str = Field(min_length=1)
    context_refs: tuple[str, ...]
    confidence: ClaimConfidence | None = None

    @field_validator("context_refs")
    @classmethod
    def context_refs_must_be_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("claim context references must be unique")
        return value

    @model_validator(mode="after")
    def evidence_claims_must_be_source_bound(self) -> CitedClaim:
        if self.kind is EvidenceClaimKind.INFERRED:
            if self.confidence is None:
                raise ValueError("inferred claims require confidence")
            if self.context_refs:
                return self
            raise ValueError("inferred claims require cited context")
        if not self.context_refs:
            raise ValueError(
                "factual, calculated, simulated, and measured claims "
                "require cited context"
            )
        if self.confidence is not None:
            raise ValueError("only inferred claims may carry confidence")
        return self


class AssistantDraft(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    disposition: RuntimeDisposition
    message: str = Field(min_length=1)
    claims: tuple[CitedClaim, ...] = ()
    questions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    forbidden_release_verdict: None = None
    prompt_manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provider_manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context_hashes: tuple[str, ...]

    @model_validator(mode="after")
    def draft_must_fail_closed_and_avoid_release_authority(self) -> AssistantDraft:
        statements = " ".join(
            [self.message, *(claim.statement for claim in self.claims)]
        ).casefold()
        if contains_release_authority_phrase(statements):
            raise ValueError(
                "assistant draft cannot produce release readiness verdicts"
            )
        if self.disposition is RuntimeDisposition.ANSWER and not self.claims:
            raise ValueError("answer drafts require at least one cited claim")
        if self.disposition is RuntimeDisposition.QUESTION and not self.questions:
            raise ValueError("question drafts require explicit questions")
        if self.disposition is RuntimeDisposition.UNKNOWN and not self.unknowns:
            raise ValueError("unknown drafts require explicit unknowns")
        return self


class RuntimeResult(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    request: ConversationRequest
    prompt_manifest: PromptManifest
    provider_manifest: ProviderManifest
    context: tuple[RetrievedContextChunk, ...]
    draft: AssistantDraft
    runtime_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def runtime_must_be_scope_and_hash_bound(self) -> RuntimeResult:
        context_ids = [chunk.context_id for chunk in self.context]
        if len(context_ids) != len(set(context_ids)):
            raise ValueError("context ids must be unique")
        context_hashes = tuple(chunk.chunk_hash for chunk in self.context)
        if self.draft.prompt_manifest_hash != self.prompt_manifest.manifest_hash:
            raise ValueError("draft prompt hash does not match prompt manifest")
        if self.draft.provider_manifest_hash != self.provider_manifest.manifest_hash:
            raise ValueError("draft provider hash does not match provider manifest")
        if self.draft.context_hashes != context_hashes:
            raise ValueError("draft context hashes do not match retrieved context")
        available_refs = {chunk.context_id for chunk in self.context}
        context_by_id = {chunk.context_id: chunk for chunk in self.context}
        for claim in self.draft.claims:
            if not set(claim.context_refs).issubset(available_refs):
                raise ValueError("claim cites context that was not retrieved")
            for context_ref in claim.context_refs:
                if not _claim_source_kind_is_compatible(
                    claim.kind, context_by_id[context_ref].source_kind
                ):
                    raise ValueError(
                        "claim kind is not compatible with cited context source"
                    )
        for chunk in self.context:
            if chunk.tenant_id != self.request.tenant_id:
                raise ValueError("retrieved context tenant does not match request")
            if chunk.project_id != self.request.project_id:
                raise ValueError("retrieved context project does not match request")
        expected = runtime_result_hash(
            self.request,
            self.prompt_manifest,
            self.provider_manifest,
            self.context,
            self.draft,
        )
        if self.runtime_hash != expected:
            raise ValueError("runtime result hash does not match payload")
        return self


class Retriever(Protocol):
    def retrieve(self, request: ConversationRequest) -> Sequence[RetrievedContextChunk]:
        """Return tenant/project-scoped context chunks for the exact request."""


class LLMProvider(Protocol):
    def draft(
        self,
        request: ConversationRequest,
        prompt_manifest: PromptManifest,
        provider_manifest: ProviderManifest,
        context: Sequence[RetrievedContextChunk],
    ) -> AssistantDraft:
        """Produce a non-authoritative assistant draft from source-bound context."""


class ConversationRuntimeService:
    def __init__(
        self,
        *,
        retriever: Retriever,
        provider: LLMProvider,
        prompt_manifest: PromptManifest,
        provider_manifest: ProviderManifest,
    ) -> None:
        self._retriever = retriever
        self._provider = provider
        self._prompt_manifest = prompt_manifest
        self._provider_manifest = provider_manifest

    def run(self, request: ConversationRequest) -> RuntimeResult:
        context = tuple(self._retriever.retrieve(request))
        context = tuple(
            RetrievedContextChunk.model_validate(chunk.model_dump(mode="python"))
            for chunk in context
        )
        scope_mismatch = any(
            chunk.tenant_id != request.tenant_id
            or chunk.project_id != request.project_id
            for chunk in context
        )
        if scope_mismatch:
            context = ()
            draft = unknown_draft(
                message="Retrieved context did not match the request scope.",
                prompt_manifest=self._prompt_manifest,
                provider_manifest=self._provider_manifest,
                context=context,
                unknowns=("retrieved context scope mismatch",),
            )
        elif not context:
            draft = question_draft(
                message="I need source-bound project context before I can answer.",
                prompt_manifest=self._prompt_manifest,
                provider_manifest=self._provider_manifest,
                context=context,
                questions=(
                    "Which CAD, BOM, firmware, protocol, or test source "
                    "should be attached?",
                ),
            )
        else:
            draft = self._provider.draft(
                request,
                self._prompt_manifest,
                self._provider_manifest,
                context,
            )
        draft = AssistantDraft.model_validate(draft.model_dump(mode="python"))
        result_hash = runtime_result_hash(
            request,
            self._prompt_manifest,
            self._provider_manifest,
            context,
            draft,
        )
        return RuntimeResult(
            request=request,
            prompt_manifest=self._prompt_manifest,
            provider_manifest=self._provider_manifest,
            context=context,
            draft=draft,
            runtime_hash=result_hash,
        )


class _PromptManifestPayload(ContractModel):
    prompt_id: str
    prompt_version: str
    system_contract: str
    safety_rules: tuple[str, ...]


class _ProviderManifestPayload(ContractModel):
    provider_id: str
    model_id: str
    model_version: str
    temperature: float
    tool_allowlist: tuple[str, ...]


class _ContextChunkPayload(ContractModel):
    context_id: str
    tenant_id: str
    project_id: str
    source_kind: str
    source_uri: str
    source_version: str
    source_captured_at: datetime
    text: str
    source_hash: str


class _RuntimeResultPayload(ContractModel):
    request: ConversationRequest
    prompt_manifest: PromptManifest
    provider_manifest: ProviderManifest
    context: tuple[RetrievedContextChunk, ...]
    draft: AssistantDraft


def _claim_source_kind_is_compatible(
    claim_kind: EvidenceClaimKind, source_kind: str
) -> bool:
    if claim_kind is EvidenceClaimKind.MEASURED:
        return source_kind in {"test", "device"}
    if claim_kind is EvidenceClaimKind.SIMULATED:
        return source_kind == "simulation"
    if claim_kind is EvidenceClaimKind.CALCULATED:
        return source_kind in {
            "cad",
            "bom",
            "firmware",
            "protocol",
            "simulation",
            "test",
            "device",
        }
    return True


def prompt_manifest_hash(
    prompt_id: str,
    prompt_version: str,
    system_contract: str,
    safety_rules: tuple[str, ...],
) -> str:
    return canonical_sha256(
        _PromptManifestPayload(
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            system_contract=system_contract,
            safety_rules=safety_rules,
        )
    )


def build_prompt_manifest(
    *,
    prompt_id: str,
    prompt_version: str,
    system_contract: str,
    safety_rules: tuple[str, ...],
) -> PromptManifest:
    return PromptManifest(
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        system_contract=system_contract,
        safety_rules=safety_rules,
        manifest_hash=prompt_manifest_hash(
            prompt_id, prompt_version, system_contract, safety_rules
        ),
    )


def provider_manifest_hash(
    provider_id: str,
    model_id: str,
    model_version: str,
    temperature: float,
    tool_allowlist: tuple[str, ...],
) -> str:
    return canonical_sha256(
        _ProviderManifestPayload(
            provider_id=provider_id,
            model_id=model_id,
            model_version=model_version,
            temperature=temperature,
            tool_allowlist=tool_allowlist,
        )
    )


def build_provider_manifest(
    *,
    provider_id: str,
    model_id: str,
    model_version: str,
    temperature: float,
    tool_allowlist: tuple[str, ...] = (),
) -> ProviderManifest:
    return ProviderManifest(
        provider_id=provider_id,
        model_id=model_id,
        model_version=model_version,
        temperature=temperature,
        tool_allowlist=tool_allowlist,
        manifest_hash=provider_manifest_hash(
            provider_id, model_id, model_version, temperature, tool_allowlist
        ),
    )


def context_chunk_hash(
    context_id: str,
    tenant_id: str,
    project_id: str,
    source_kind: str,
    source_uri: str,
    source_version: str,
    source_captured_at: datetime,
    text: str,
    source_hash: str,
) -> str:
    return canonical_sha256(
        _ContextChunkPayload(
            context_id=context_id,
            tenant_id=tenant_id,
            project_id=project_id,
            source_kind=source_kind,
            source_uri=source_uri,
            source_version=source_version,
            source_captured_at=source_captured_at,
            text=text,
            source_hash=source_hash,
        )
    )


def build_context_chunk(
    *,
    context_id: str,
    tenant_id: str,
    project_id: str,
    source_kind: Literal[
        "cad",
        "bom",
        "firmware",
        "protocol",
        "test",
        "document",
        "simulation",
        "device",
        "user",
    ],
    source_uri: str,
    source_version: str,
    source_captured_at: datetime,
    text: str,
    source_hash: str,
) -> RetrievedContextChunk:
    return RetrievedContextChunk(
        context_id=context_id,
        tenant_id=tenant_id,
        project_id=project_id,
        source_kind=source_kind,
        source_uri=source_uri,
        source_version=source_version,
        source_captured_at=source_captured_at,
        text=text,
        source_hash=source_hash,
        chunk_hash=context_chunk_hash(
            context_id,
            tenant_id,
            project_id,
            source_kind,
            source_uri,
            source_version,
            source_captured_at,
            text,
            source_hash,
        ),
    )


def question_draft(
    *,
    message: str,
    prompt_manifest: PromptManifest,
    provider_manifest: ProviderManifest,
    context: Sequence[RetrievedContextChunk],
    questions: tuple[str, ...],
) -> AssistantDraft:
    return AssistantDraft(
        disposition=RuntimeDisposition.QUESTION,
        message=message,
        questions=questions,
        prompt_manifest_hash=prompt_manifest.manifest_hash,
        provider_manifest_hash=provider_manifest.manifest_hash,
        context_hashes=tuple(chunk.chunk_hash for chunk in context),
    )


def unknown_draft(
    *,
    message: str,
    prompt_manifest: PromptManifest,
    provider_manifest: ProviderManifest,
    context: Sequence[RetrievedContextChunk],
    unknowns: tuple[str, ...],
) -> AssistantDraft:
    return AssistantDraft(
        disposition=RuntimeDisposition.UNKNOWN,
        message=message,
        unknowns=unknowns,
        prompt_manifest_hash=prompt_manifest.manifest_hash,
        provider_manifest_hash=provider_manifest.manifest_hash,
        context_hashes=tuple(chunk.chunk_hash for chunk in context),
    )


def runtime_result_hash(
    request: ConversationRequest,
    prompt_manifest: PromptManifest,
    provider_manifest: ProviderManifest,
    context: tuple[RetrievedContextChunk, ...],
    draft: AssistantDraft,
) -> str:
    return canonical_sha256(
        _RuntimeResultPayload(
            request=request,
            prompt_manifest=prompt_manifest,
            provider_manifest=provider_manifest,
            context=context,
            draft=draft,
        )
    )
