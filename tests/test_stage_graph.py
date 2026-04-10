"""Tests for stage graph parsing and navigation."""

from __future__ import annotations

import pytest

from build_your_room.stage_graph import (
    ReviewConfig,
    StageGraph,
    StageGraphError,
    StageNode,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_GRAPH_JSON = {
    "entry_stage": "spec_author",
    "nodes": [
        {
            "key": "spec_author",
            "name": "Spec authoring",
            "type": "spec_author",
            "agent": "claude",
            "prompt": "spec_author_default",
            "model": "claude-opus-4-6",
            "max_iterations": 1,
            "context_threshold_pct": 60,
        },
    ],
    "edges": [
        {
            "key": "spec_to_done",
            "from": "spec_author",
            "to": "completed",
            "on": "approved",
        },
    ],
}


def _full_graph_json() -> dict:
    """The full pipeline graph from the spec."""
    return {
        "entry_stage": "spec_author",
        "nodes": [
            {
                "key": "spec_author",
                "name": "Spec authoring",
                "type": "spec_author",
                "agent": "claude",
                "prompt": "spec_author_default",
                "model": "claude-opus-4-6",
                "max_iterations": 1,
                "review": {
                    "agent": "codex",
                    "prompt": "spec_review_default",
                    "model": "gpt-5.1-codex",
                    "max_review_rounds": 5,
                    "exit_condition": "structured_approval",
                    "on_max_rounds": "escalate",
                },
                "context_threshold_pct": 60,
            },
            {
                "key": "impl_plan",
                "name": "Implementation plan",
                "type": "impl_plan",
                "agent": "claude",
                "prompt": "impl_plan_default",
                "model": "claude-opus-4-6",
                "max_iterations": 1,
                "review": {
                    "agent": "codex",
                    "prompt": "impl_plan_review_default",
                    "model": "gpt-5.1-codex",
                    "max_review_rounds": 5,
                    "exit_condition": "structured_approval",
                    "on_max_rounds": "escalate",
                },
                "context_threshold_pct": 60,
            },
            {
                "key": "impl_task",
                "name": "Implementation",
                "type": "impl_task",
                "agent": "claude",
                "prompt": "impl_task_default",
                "model": "claude-sonnet-4-6",
                "max_iterations": 50,
                "context_threshold_pct": 60,
                "on_context_limit": "resume_current_claim",
            },
            {
                "key": "code_review",
                "name": "Code review + bug fix",
                "type": "code_review",
                "agent": "codex",
                "prompt": "code_review_default",
                "model": "gpt-5.1-codex",
                "max_iterations": 3,
                "fix_agent": "codex",
                "fix_prompt": "bug_fix_default",
            },
            {
                "key": "validation",
                "name": "Validation",
                "type": "validation",
                "agent": "claude",
                "prompt": "validation_default",
                "model": "claude-sonnet-4-6",
                "max_iterations": 3,
                "uses_devbrowser": True,
                "record_on_success": True,
            },
        ],
        "edges": [
            {"key": "spec_to_plan", "from": "spec_author", "to": "impl_plan", "on": "approved"},
            {"key": "plan_to_impl", "from": "impl_plan", "to": "impl_task", "on": "approved"},
            {
                "key": "impl_to_review",
                "from": "impl_task",
                "to": "code_review",
                "on": "stage_complete",
            },
            {
                "key": "review_to_validation",
                "from": "code_review",
                "to": "validation",
                "on": "approved",
            },
            {
                "key": "validation_back_to_review",
                "from": "validation",
                "to": "code_review",
                "on": "validation_failed",
                "max_visits": 3,
                "on_exhausted": "escalate",
            },
            {
                "key": "validation_to_done",
                "from": "validation",
                "to": "completed",
                "on": "validated",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------


class TestStageGraphParsing:
    def test_parse_minimal_graph(self):
        graph = StageGraph.from_json(MINIMAL_GRAPH_JSON)
        assert graph.entry_stage == "spec_author"
        assert len(graph.nodes) == 1
        assert len(graph.edges) == 1

    def test_parse_full_graph(self):
        graph = StageGraph.from_json(_full_graph_json())
        assert graph.entry_stage == "spec_author"
        assert len(graph.nodes) == 5
        assert len(graph.edges) == 6

    def test_node_types_parsed_correctly(self):
        graph = StageGraph.from_json(_full_graph_json())
        spec = graph.get_node("spec_author")
        assert isinstance(spec, StageNode)
        assert spec.stage_type == "spec_author"
        assert spec.agent == "claude"
        assert spec.model == "claude-opus-4-6"
        assert spec.max_iterations == 1
        assert spec.context_threshold_pct == 60

    def test_review_config_parsed(self):
        graph = StageGraph.from_json(_full_graph_json())
        spec = graph.get_node("spec_author")
        assert spec.review is not None
        assert isinstance(spec.review, ReviewConfig)
        assert spec.review.agent == "codex"
        assert spec.review.max_review_rounds == 5
        assert spec.review.exit_condition == "structured_approval"
        assert spec.review.on_max_rounds == "escalate"

    def test_node_without_review(self):
        graph = StageGraph.from_json(_full_graph_json())
        impl = graph.get_node("impl_task")
        assert impl.review is None
        assert impl.on_context_limit == "resume_current_claim"

    def test_optional_fields(self):
        graph = StageGraph.from_json(_full_graph_json())
        cr = graph.get_node("code_review")
        assert cr.fix_agent == "codex"
        assert cr.fix_prompt == "bug_fix_default"

        val = graph.get_node("validation")
        assert val.uses_devbrowser is True
        assert val.record_on_success is True

    def test_edge_with_max_visits(self):
        graph = StageGraph.from_json(_full_graph_json())
        back_edge = [e for e in graph.edges if e.key == "validation_back_to_review"][0]
        assert back_edge.max_visits == 3
        assert back_edge.on_exhausted == "escalate"

    def test_context_threshold_default(self):
        data = {
            "entry_stage": "s1",
            "nodes": [
                {
                    "key": "s1",
                    "name": "S1",
                    "type": "custom",
                    "agent": "claude",
                    "prompt": "p",
                    "model": "m",
                    "max_iterations": 1,
                    # no context_threshold_pct
                },
            ],
            "edges": [],
        }
        graph = StageGraph.from_json(data)
        assert graph.get_node("s1").context_threshold_pct == 60


# ---------------------------------------------------------------------------
# Validation error tests
# ---------------------------------------------------------------------------


class TestStageGraphValidation:
    def test_missing_entry_stage(self):
        with pytest.raises(StageGraphError, match="missing 'entry_stage'"):
            StageGraph.from_json({"nodes": [{"key": "x"}]})

    def test_missing_nodes(self):
        with pytest.raises(StageGraphError, match="missing 'nodes'"):
            StageGraph.from_json({"entry_stage": "x"})

    def test_empty_nodes(self):
        with pytest.raises(StageGraphError, match="missing 'nodes'"):
            StageGraph.from_json({"entry_stage": "x", "nodes": []})

    def test_entry_stage_not_in_nodes(self):
        data = {
            "entry_stage": "missing",
            "nodes": [
                {
                    "key": "s1",
                    "name": "S1",
                    "type": "custom",
                    "agent": "claude",
                    "prompt": "p",
                    "model": "m",
                    "max_iterations": 1,
                },
            ],
        }
        with pytest.raises(StageGraphError, match="not found in nodes"):
            StageGraph.from_json(data)

    def test_duplicate_node_key(self):
        node = {
            "key": "dup",
            "name": "D",
            "type": "custom",
            "agent": "claude",
            "prompt": "p",
            "model": "m",
            "max_iterations": 1,
        }
        data = {"entry_stage": "dup", "nodes": [node, node]}
        with pytest.raises(StageGraphError, match="duplicate node key"):
            StageGraph.from_json(data)

    def test_duplicate_edge_key(self):
        data = {
            "entry_stage": "s1",
            "nodes": [
                {
                    "key": "s1",
                    "name": "S1",
                    "type": "custom",
                    "agent": "claude",
                    "prompt": "p",
                    "model": "m",
                    "max_iterations": 1,
                },
            ],
            "edges": [
                {"key": "e1", "from": "s1", "to": "completed", "on": "done"},
                {"key": "e1", "from": "s1", "to": "completed", "on": "fail"},
            ],
        }
        with pytest.raises(StageGraphError, match="duplicate edge key"):
            StageGraph.from_json(data)

    def test_edge_references_unknown_from(self):
        data = {
            "entry_stage": "s1",
            "nodes": [
                {
                    "key": "s1",
                    "name": "S1",
                    "type": "custom",
                    "agent": "claude",
                    "prompt": "p",
                    "model": "m",
                    "max_iterations": 1,
                },
            ],
            "edges": [
                {"key": "e1", "from": "nonexistent", "to": "completed", "on": "done"},
            ],
        }
        with pytest.raises(StageGraphError, match="unknown from_stage"):
            StageGraph.from_json(data)

    def test_edge_references_unknown_to(self):
        data = {
            "entry_stage": "s1",
            "nodes": [
                {
                    "key": "s1",
                    "name": "S1",
                    "type": "custom",
                    "agent": "claude",
                    "prompt": "p",
                    "model": "m",
                    "max_iterations": 1,
                },
            ],
            "edges": [
                {"key": "e1", "from": "s1", "to": "nonexistent", "on": "done"},
            ],
        }
        with pytest.raises(StageGraphError, match="unknown to_stage"):
            StageGraph.from_json(data)


# ---------------------------------------------------------------------------
# Navigation tests
# ---------------------------------------------------------------------------


class TestStageGraphNavigation:
    def test_get_node(self):
        graph = StageGraph.from_json(_full_graph_json())
        node = graph.get_node("impl_task")
        assert node.key == "impl_task"
        assert node.max_iterations == 50

    def test_get_node_not_found(self):
        graph = StageGraph.from_json(MINIMAL_GRAPH_JSON)
        with pytest.raises(KeyError):
            graph.get_node("nonexistent")

    def test_get_outgoing_edges(self):
        graph = StageGraph.from_json(_full_graph_json())
        edges = graph.get_outgoing_edges("validation")
        assert len(edges) == 2
        edge_keys = {e.key for e in edges}
        assert edge_keys == {"validation_back_to_review", "validation_to_done"}

    def test_get_outgoing_edges_empty(self):
        graph = StageGraph.from_json(MINIMAL_GRAPH_JSON)
        # spec_author has one edge
        edges = graph.get_outgoing_edges("spec_author")
        assert len(edges) == 1

    def test_resolve_next_stage_simple(self):
        graph = StageGraph.from_json(_full_graph_json())
        next_key, edge = graph.resolve_next_stage("spec_author", "approved", {})
        assert next_key == "impl_plan"
        assert edge is not None
        assert edge.key == "spec_to_plan"

    def test_resolve_next_stage_no_match(self):
        graph = StageGraph.from_json(_full_graph_json())
        next_key, edge = graph.resolve_next_stage("spec_author", "unknown_result", {})
        assert next_key is None
        assert edge is None

    def test_resolve_next_stage_to_completed(self):
        graph = StageGraph.from_json(_full_graph_json())
        next_key, edge = graph.resolve_next_stage("validation", "validated", {})
        assert next_key == "completed"
        assert edge is not None
        assert edge.key == "validation_to_done"

    def test_resolve_with_max_visits_under_limit(self):
        graph = StageGraph.from_json(_full_graph_json())
        next_key, edge = graph.resolve_next_stage(
            "validation", "validation_failed", {"validation_back_to_review": 1}
        )
        assert next_key == "code_review"
        assert edge is not None

    def test_resolve_with_max_visits_at_limit_escalate(self):
        graph = StageGraph.from_json(_full_graph_json())
        next_key, edge = graph.resolve_next_stage(
            "validation", "validation_failed", {"validation_back_to_review": 3}
        )
        # Exhausted with on_exhausted=escalate -> None next_key, edge returned for caller
        assert next_key is None
        assert edge is not None
        assert edge.key == "validation_back_to_review"

    def test_resolve_with_max_visits_no_escalation_handler_raises(self):
        data = {
            "entry_stage": "a",
            "nodes": [
                {
                    "key": "a",
                    "name": "A",
                    "type": "custom",
                    "agent": "claude",
                    "prompt": "p",
                    "model": "m",
                    "max_iterations": 1,
                },
                {
                    "key": "b",
                    "name": "B",
                    "type": "custom",
                    "agent": "claude",
                    "prompt": "p",
                    "model": "m",
                    "max_iterations": 1,
                },
            ],
            "edges": [
                {
                    "key": "loop",
                    "from": "a",
                    "to": "b",
                    "on": "retry",
                    "max_visits": 2,
                    # no on_exhausted
                },
            ],
        }
        graph = StageGraph.from_json(data)
        with pytest.raises(StageGraphError, match="exhausted"):
            graph.resolve_next_stage("a", "retry", {"loop": 2})

    def test_full_pipeline_traversal(self):
        """Walk the happy path through all stages."""
        graph = StageGraph.from_json(_full_graph_json())
        visit_counts: dict[str, int] = {}

        current = graph.entry_stage
        path = [current]

        results = ["approved", "approved", "stage_complete", "approved", "validated"]
        for result in results:
            next_key, edge = graph.resolve_next_stage(current, result, visit_counts)
            assert next_key is not None
            assert edge is not None
            visit_counts[edge.key] = visit_counts.get(edge.key, 0) + 1
            current = next_key
            path.append(current)

        assert path == [
            "spec_author",
            "impl_plan",
            "impl_task",
            "code_review",
            "validation",
            "completed",
        ]
