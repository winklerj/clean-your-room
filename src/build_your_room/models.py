from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Repo:
    id: int
    name: str
    local_path: str
    git_url: str | None
    default_branch: str
    created_at: datetime
    archived: int


@dataclass
class Prompt:
    id: int
    name: str
    body: str
    stage_type: str
    agent_type: str
    created_at: datetime
    updated_at: datetime


@dataclass
class PipelineDef:
    id: int
    name: str
    stage_graph_json: str
    created_at: datetime


@dataclass
class Pipeline:
    id: int
    pipeline_def_id: int
    repo_id: int
    clone_path: str
    workspace_ref: str | None
    review_base_rev: str
    head_rev: str | None
    workspace_state: str
    dirty_snapshot_artifact: str | None
    status: str
    current_stage_key: str | None
    owner_token: str | None
    last_heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    recovery_state_json: str | None
    config_json: str
    created_at: datetime
    updated_at: datetime


@dataclass
class PipelineStage:
    id: int
    pipeline_id: int
    stage_key: str
    attempt: int
    entry_edge_key: str | None
    stage_type: str
    agent_type: str
    status: str
    entry_rev: str | None
    exit_rev: str | None
    iteration: int
    max_iterations: int
    output_artifact: str | None
    escalation_reason: str | None
    owner_token: str | None
    last_heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass
class AgentSession:
    id: int
    pipeline_stage_id: int
    session_type: str
    session_id: str | None
    prompt_id: int | None
    prompt_override: str | None
    status: str
    context_usage_pct: float | None
    cost_usd: float
    token_input: int
    token_output: int
    resume_state_json: str | None
    owner_token: str | None
    last_heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    started_at: datetime
    completed_at: datetime | None


@dataclass
class SessionLog:
    id: int
    agent_session_id: int
    event_type: str
    content: str
    created_at: datetime


@dataclass
class Escalation:
    id: int
    pipeline_id: int
    pipeline_stage_id: int | None
    reason: str
    context_json: str
    status: str
    resolution: str | None
    created_at: datetime
    resolved_at: datetime | None


@dataclass
class HtnTask:
    id: int
    pipeline_id: int
    parent_task_id: int | None
    name: str
    description: str
    task_type: str
    status: str
    priority: int
    ordering: int
    assigned_session_id: int | None
    claim_token: str | None
    claim_owner_token: str | None
    claim_expires_at: datetime | None
    preconditions_json: str
    postconditions_json: str
    invariants_json: str | None
    output_artifacts_json: str | None
    checkpoint_rev: str | None
    estimated_complexity: str | None
    diary_entry: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass
class HtnTaskDep:
    id: int
    task_id: int
    depends_on_task_id: int
    dep_type: str = field(default="hard")
