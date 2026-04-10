"""Stage graph types and navigation for pipeline definitions.

Parses the stage_graph_json from pipeline_defs into typed structures
and provides edge resolution for stage transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReviewConfig:
    """Review sub-config for a stage node."""

    agent: str
    prompt: str
    model: str
    max_review_rounds: int
    exit_condition: str  # 'structured_approval', 'proceed_with_warnings'
    on_max_rounds: str  # 'escalate', 'proceed_with_warnings'


@dataclass(frozen=True)
class StageNode:
    """A node in the pipeline stage graph."""

    key: str
    name: str
    stage_type: str
    agent: str  # 'claude' or 'codex'
    prompt: str
    model: str
    max_iterations: int
    context_threshold_pct: int = 60
    review: ReviewConfig | None = None
    on_context_limit: str | None = None  # 'resume_current_claim', 'new_session_continue', 'escalate'
    fix_agent: str | None = None
    fix_prompt: str | None = None
    uses_devbrowser: bool = False
    record_on_success: bool = False


@dataclass(frozen=True)
class StageEdge:
    """A directed edge in the pipeline stage graph."""

    key: str
    from_stage: str
    to_stage: str
    on: str  # guard condition: 'approved', 'stage_complete', 'validation_failed', 'validated'
    max_visits: int | None = None
    on_exhausted: str | None = None  # 'escalate'


class StageGraphError(Exception):
    """Raised for invalid stage graph configurations."""


@dataclass
class StageGraph:
    """Parsed stage graph with navigation methods.

    Immutable after construction — call from_json() to build.
    """

    entry_stage: str
    nodes: dict[str, StageNode] = field(default_factory=dict)
    edges: list[StageEdge] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> StageGraph:
        """Parse a stage_graph_json dict into a typed StageGraph.

        Raises StageGraphError on validation failures.
        """
        entry = data.get("entry_stage")
        if not entry:
            raise StageGraphError("stage graph missing 'entry_stage'")

        raw_nodes = data.get("nodes")
        if not raw_nodes:
            raise StageGraphError("stage graph missing 'nodes'")

        nodes: dict[str, StageNode] = {}
        for n in raw_nodes:
            review_data = n.get("review")
            review = (
                ReviewConfig(
                    agent=review_data["agent"],
                    prompt=review_data["prompt"],
                    model=review_data["model"],
                    max_review_rounds=review_data["max_review_rounds"],
                    exit_condition=review_data.get("exit_condition", "structured_approval"),
                    on_max_rounds=review_data.get("on_max_rounds", "escalate"),
                )
                if review_data
                else None
            )
            node = StageNode(
                key=n["key"],
                name=n["name"],
                stage_type=n["type"],
                agent=n["agent"],
                prompt=n["prompt"],
                model=n["model"],
                max_iterations=n["max_iterations"],
                context_threshold_pct=n.get("context_threshold_pct", 60),
                review=review,
                on_context_limit=n.get("on_context_limit"),
                fix_agent=n.get("fix_agent"),
                fix_prompt=n.get("fix_prompt"),
                uses_devbrowser=n.get("uses_devbrowser", False),
                record_on_success=n.get("record_on_success", False),
            )
            if node.key in nodes:
                raise StageGraphError(f"duplicate node key: {node.key!r}")
            nodes[node.key] = node

        if entry not in nodes:
            raise StageGraphError(
                f"entry_stage {entry!r} not found in nodes"
            )

        raw_edges = data.get("edges", [])
        edge_keys: set[str] = set()
        edges: list[StageEdge] = []
        for e in raw_edges:
            edge = StageEdge(
                key=e["key"],
                from_stage=e["from"],
                to_stage=e["to"],
                on=e["on"],
                max_visits=e.get("max_visits"),
                on_exhausted=e.get("on_exhausted"),
            )
            if edge.key in edge_keys:
                raise StageGraphError(f"duplicate edge key: {edge.key!r}")
            # 'completed' is a virtual terminal node, not in nodes
            if edge.from_stage not in nodes:
                raise StageGraphError(
                    f"edge {edge.key!r} references unknown from_stage {edge.from_stage!r}"
                )
            if edge.to_stage not in nodes and edge.to_stage != "completed":
                raise StageGraphError(
                    f"edge {edge.key!r} references unknown to_stage {edge.to_stage!r}"
                )
            edge_keys.add(edge.key)
            edges.append(edge)

        return cls(entry_stage=entry, nodes=nodes, edges=edges)

    def get_node(self, key: str) -> StageNode:
        """Get a stage node by key. Raises KeyError if not found."""
        return self.nodes[key]

    def get_outgoing_edges(self, stage_key: str) -> list[StageEdge]:
        """Return all edges originating from a given stage."""
        return [e for e in self.edges if e.from_stage == stage_key]

    def resolve_next_stage(
        self,
        current_key: str,
        result: str,
        visit_counts: dict[str, int],
    ) -> tuple[str | None, StageEdge | None]:
        """Determine the next stage given the current stage's result.

        Args:
            current_key: The key of the stage that just completed.
            result: The outcome string (e.g. 'approved', 'validation_failed').
            visit_counts: Map of edge_key -> times this edge has been traversed.

        Returns:
            (next_stage_key, edge) — next_stage_key is None if no matching
            edge is found, and 'completed' if the pipeline should finish.
            edge is None when no matching edge exists.

        Raises:
            StageGraphError: if a matching edge is exhausted and on_exhausted
                is not 'escalate' (i.e., a misconfigured graph).
        """
        for edge in self.get_outgoing_edges(current_key):
            if edge.on != result:
                continue

            if edge.max_visits is not None:
                visits = visit_counts.get(edge.key, 0)
                if visits >= edge.max_visits:
                    if edge.on_exhausted == "escalate":
                        return None, edge  # caller should escalate
                    raise StageGraphError(
                        f"edge {edge.key!r} exhausted ({visits}/{edge.max_visits}) "
                        f"with no escalation handler"
                    )

            return edge.to_stage, edge

        return None, None
