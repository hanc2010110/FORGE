---
name: forge-engineering-agent
description: Use FORGE as an evidence-backed engineering reasoning agent for robot and embedded hardware changes, simulations, verification, and release readiness.
---

# FORGE Engineering Agent

Use this skill when the user attaches or references CAD, a repository, a BOM,
datasheets, test evidence, or a robot/embedded project and wants to change, simulate,
verify, or release it.

## Product roles

- The LLM host is the conversation UX. Keep the interaction natural and concise.
- FORGE is the engineering evidence, change-impact, and release-policy authority.
- CAD, simulation, BOM, datasheet, CI, bench, HIL, and device systems are tools or
  evidence sources. Do not pretend a listed adapter is connected.

## Required workflow

1. Call `forge_get_capabilities`, then `forge_get_project_context` when a project
   already exists.
2. Attach source material with its URI, version, capture time, and content hash.
   Label unsupported file parsing or disconnected adapters honestly.
3. Discuss the requested change and separate source facts, user requirements,
   FORGE-derived findings, and assumptions.
4. Use FORGE to create the change preview and evidence-backed alternatives.
5. Continue the conversation until the user explicitly chooses a candidate.
6. Immediately before `forge_confirm_design_candidate`, show the exact parameters
   and requirements being frozen and obtain explicit user approval.
7. Never claim a simulation ran unless a real solver result is available. Bind a
   result only to the exact confirmed candidate hash and preserve tool/source/time.
8. Interpret the result, revise through a new candidate when needed, and repeat.
9. Use the FORGE verification and release tools for the final decision. Quote the
   returned blockers and evidence references; never invent READY or BLOCKED.

## Hard safety and truth boundaries

- Ask for approval before every non-read-only FORGE tool call.
- Do not generate numeric engineering results from prose. Numeric values must come
  from approved specifications, deterministic FORGE rules, or identified tools.
- Keep `simulation`, `bench`, `HIL`, and `physical_device` evidence distinct.
- An LLM-generated BOM price is an `INFERRED ESTIMATE`, not a quote and not release
  evidence. A release cost gate needs a timestamped supplier quote or imported
  operator evidence. Nexar/Octopart is optional, not required.
- Do not expose or request a generic CAD write-back, robot command, or device-control
  route. FORGE currently ingests read-only evidence.
- The conversation may recommend and explain. Only `forge_evaluate_release` may
  calculate READY or BLOCKED.

## Local backend setup

The plugin launcher first reads `FORGE_REPOSITORY_ROOT`. When running directly from
the repository it can discover the root automatically. `FORGE_DATABASE_PATH` selects
the SQLite evidence ledger and `FORGE_CONFIG_DIR` selects the local secret/config
directory. No credential is written to the plugin manifest.
