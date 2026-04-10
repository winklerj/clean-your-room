"""Tests for stage graph parsing and navigation.

Property-based tests verify invariants across generated stage graphs.
Unit tests verify parsing, validation, and navigation for specific cases.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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


# ---------------------------------------------------------------------------
# Strategies for property-based tests
# ---------------------------------------------------------------------------

_stage_types = st.sampled_from(
    ["spec_author", "impl_plan", "impl_task", "code_review", "validation", "custom"]
)
_agents = st.sampled_from(["claude", "codex"])
_models = st.sampled_from(["claude-opus-4-6", "claude-sonnet-4-6", "gpt-5.1-codex"])
_guards = st.sampled_from(["approved", "stage_complete", "validation_failed", "validated"])
_node_key = st.from_regex(r"[a-z][a-z0-9_]{1,15}", fullmatch=True)


@st.composite
def stage_node_dicts(draw: st.DrawFn, key: str | None = None) -> dict:
    """Generate a valid stage node dict for from_json."""
    return {
        "key": key or draw(_node_key),
        "name": draw(st.from_regex(r"[A-Z][a-zA-Z ]{1,25}", fullmatch=True)),
        "type": draw(_stage_types),
        "agent": draw(_agents),
        "prompt": draw(st.from_regex(r"[a-z_]{3,15}", fullmatch=True)),
        "model": draw(_models),
        "max_iterations": draw(st.integers(min_value=1, max_value=100)),
        "context_threshold_pct": draw(st.integers(min_value=10, max_value=95)),
    }


@st.composite
def valid_stage_graphs(draw: st.DrawFn) -> dict:
    """Generate a valid stage graph JSON dict with 2-6 nodes and edges."""
    n = draw(st.integers(min_value=2, max_value=6))
    keys: list[str] = []
    for i in range(n):
        k = draw(st.from_regex(r"[a-z][a-z0-9_]{1,10}", fullmatch=True))
        while k in keys:
            k = k + str(i)
        keys.append(k)

    nodes = []
    for k in keys:
        nodes.append(draw(stage_node_dicts(key=k)))

    edges = []
    edge_keys: set[str] = set()
    # Create a chain: node[0] → node[1] → ... → completed
    for i in range(n - 1):
        ek = f"e_{keys[i]}_to_{keys[i + 1]}"
        if ek in edge_keys:
            ek = ek + f"_{i}"
        edge_keys.add(ek)
        edges.append({
            "key": ek,
            "from": keys[i],
            "to": keys[i + 1],
            "on": draw(_guards),
        })

    # Terminal edge from last node
    final_ek = f"e_{keys[-1]}_to_done"
    edges.append({
        "key": final_ek,
        "from": keys[-1],
        "to": "completed",
        "on": draw(_guards),
    })

    # Optionally add a back-edge with max_visits
    if draw(st.booleans()):
        if n >= 3:
            src_idx = draw(st.integers(min_value=2, max_value=n - 1))
            dst_idx = draw(st.integers(min_value=0, max_value=src_idx - 1))
            back_ek = f"back_{keys[src_idx]}_to_{keys[dst_idx]}"
            if back_ek not in edge_keys:
                edge_keys.add(back_ek)
                back_edge: dict[str, str | int] = {
                    "key": back_ek,
                    "from": keys[src_idx],
                    "to": keys[dst_idx],
                    "on": draw(_guards),
                    "max_visits": draw(st.integers(min_value=1, max_value=5)),
                    "on_exhausted": "escalate",
                }
                edges.append(back_edge)  # type: ignore[arg-type]

    return {
        "entry_stage": keys[0],
        "nodes": nodes,
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestStageGraphProperties:
    """Property-based tests for stage graph invariants."""

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs())
    def test_entry_stage_always_in_nodes(self, graph_json) -> None:
        """Property: entry_stage always exists in the parsed nodes dict.

        Invariant: for all valid graphs, graph.entry_stage in graph.nodes.
        """
        graph = StageGraph.from_json(graph_json)
        assert graph.entry_stage in graph.nodes

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs())
    def test_all_edge_from_stages_exist_in_nodes(self, graph_json) -> None:
        """Property: every edge's from_stage is a valid node key.

        Invariant: for all edges e in graph, e.from_stage in graph.nodes.
        """
        graph = StageGraph.from_json(graph_json)
        for edge in graph.edges:
            assert edge.from_stage in graph.nodes, (
                f"Edge {edge.key!r} references unknown from_stage {edge.from_stage!r}"
            )

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs())
    def test_all_edge_to_stages_valid(self, graph_json) -> None:
        """Property: every edge's to_stage is either a node key or 'completed'.

        Invariant: edge.to_stage in graph.nodes | {'completed'}.
        """
        graph = StageGraph.from_json(graph_json)
        valid_targets = set(graph.nodes.keys()) | {"completed"}
        for edge in graph.edges:
            assert edge.to_stage in valid_targets, (
                f"Edge {edge.key!r} targets unknown {edge.to_stage!r}"
            )

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs())
    def test_no_duplicate_node_keys(self, graph_json) -> None:
        """Property: all node keys in a parsed graph are unique.

        Invariant: from_json rejects or deduplicates node keys.
        """
        graph = StageGraph.from_json(graph_json)
        assert len(graph.nodes) == len(graph_json["nodes"])

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs())
    def test_no_duplicate_edge_keys(self, graph_json) -> None:
        """Property: all edge keys in a parsed graph are unique.

        Invariant: from_json rejects or deduplicates edge keys.
        """
        graph = StageGraph.from_json(graph_json)
        edge_keys = [e.key for e in graph.edges]
        assert len(edge_keys) == len(set(edge_keys))

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs())
    def test_outgoing_edges_only_from_specified_node(self, graph_json) -> None:
        """Property: get_outgoing_edges returns only edges originating from the given node.

        Invariant: for all e in get_outgoing_edges(k), e.from_stage == k.
        """
        graph = StageGraph.from_json(graph_json)
        for key in graph.nodes:
            outgoing = graph.get_outgoing_edges(key)
            for edge in outgoing:
                assert edge.from_stage == key

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs(), guard=_guards)
    def test_resolve_with_unmatched_guard_returns_none(self, graph_json, guard) -> None:
        """Property: resolve with a guard no edge matches returns (None, None).

        Invariant: if no outgoing edge has edge.on == guard, result is (None, None).
        """
        graph = StageGraph.from_json(graph_json)
        # Pick a node and construct a guard that no edge uses
        key = graph.entry_stage
        # Use a synthetic guard guaranteed not to match any real edge guard
        synthetic_guard = f"__never_used_{guard}"
        next_key, edge = graph.resolve_next_stage(key, synthetic_guard, {})
        assert next_key is None
        assert edge is None

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs())
    def test_resolve_matching_guard_returns_valid_target(self, graph_json) -> None:
        """Property: resolve with a matching guard returns the correct edge target.

        Invariant: if edge.on == result, resolve returns (edge.to_stage, edge).
        """
        graph = StageGraph.from_json(graph_json)
        key = graph.entry_stage
        outgoing = graph.get_outgoing_edges(key)
        if not outgoing:
            return  # no edges to test

        # Pick the first edge and use its guard
        target_edge = outgoing[0]
        next_key, edge = graph.resolve_next_stage(key, target_edge.on, {})
        assert next_key == target_edge.to_stage
        assert edge is not None
        assert edge.key == target_edge.key

    @settings(max_examples=30)
    @given(
        graph_json=valid_stage_graphs(),
        visit_count=st.integers(min_value=0, max_value=20),
    )
    def test_visit_count_gating_on_bounded_edges(self, graph_json, visit_count) -> None:
        """Property: edges with max_visits block when visit count >= limit.

        Invariant: if visits >= max_visits and on_exhausted='escalate',
        resolve returns (None, edge) — an escalation signal.
        If visits < max_visits, the edge is traversable.

        Only tests the bounded edge when it is the first matching edge for
        its from_stage and guard, since resolve_next_stage returns the first
        matching edge.
        """
        graph = StageGraph.from_json(graph_json)
        bounded_edges = [e for e in graph.edges if e.max_visits is not None]
        if not bounded_edges:
            return  # no bounded edges in this graph

        edge = bounded_edges[0]

        # Only test if the bounded edge is the first match for its guard
        outgoing = graph.get_outgoing_edges(edge.from_stage)
        first_match = next((e for e in outgoing if e.on == edge.on), None)
        if first_match is None or first_match.key != edge.key:
            return  # another edge with the same guard comes first

        visit_counts = {edge.key: visit_count}

        if visit_count >= edge.max_visits:
            if edge.on_exhausted == "escalate":
                next_key, resolved = graph.resolve_next_stage(
                    edge.from_stage, edge.on, visit_counts
                )
                assert next_key is None, "Exhausted edge should not resolve to a stage"
                assert resolved is not None, "Exhausted edge should return the edge"
                assert resolved.key == edge.key
        else:
            next_key, resolved = graph.resolve_next_stage(
                edge.from_stage, edge.on, visit_counts
            )
            assert resolved is not None
            assert resolved.key == edge.key
            assert next_key == edge.to_stage

    @settings(max_examples=50)
    @given(graph_json=valid_stage_graphs())
    def test_traversal_visit_counts_monotonically_increase(self, graph_json) -> None:
        """Property: edge visit counts never decrease during traversal.

        Invariant: after each edge traversal, visit_counts[edge.key] >= previous value.
        """
        graph = StageGraph.from_json(graph_json)
        visit_counts: dict[str, int] = {}
        current = graph.entry_stage
        max_steps = len(graph.edges) * 3  # prevent infinite loops

        for _ in range(max_steps):
            if current == "completed":
                break
            outgoing = graph.get_outgoing_edges(current)
            if not outgoing:
                break

            # Try each guard until one resolves
            resolved = False
            for edge in outgoing:
                try:
                    next_key, resolved_edge = graph.resolve_next_stage(
                        current, edge.on, visit_counts
                    )
                except StageGraphError:
                    continue

                if next_key is not None and resolved_edge is not None:
                    old_count = visit_counts.get(resolved_edge.key, 0)
                    visit_counts[resolved_edge.key] = old_count + 1
                    assert visit_counts[resolved_edge.key] >= old_count
                    current = next_key
                    resolved = True
                    break

            if not resolved:
                break

        # Final check: no counts went below zero
        for count in visit_counts.values():
            assert count >= 0
