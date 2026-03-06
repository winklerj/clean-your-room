# Testing Strategy

## Overview

This project employs a testing philosophy centered on **property-based testing** to ensure correctness across the full input space, complemented by targeted unit and integration tests for specific scenarios and edge cases.

### Core Principles

1. **Property-Based Testing First**: Prefer property tests that verify invariants over example-based
   tests that check specific cases
2. **Document Every Test**: Each test MUST include documentation explaining why it's important and
   what invariant it verifies (per AGENTS.md)
3. **Structured Logging**: All production code uses structured logging (tracing) for observability
4. **Fail Fast, Fail Clearly**: Tests should produce clear error messages that identify the root
   cause
## Test Categories

### Unit Tests (Per-Module)

Standard `def test_*` functions for synchronous, isolated logic testing. Located in `tests/` directories
or alongside source files as `test_*.py`.

### Property-Based Tests (Hypothesis)

Generative tests that verify invariants hold across randomly generated inputs. Use the `@given`
decorator from the `hypothesis` library.

### Integration Tests

Tests that exercise complete workflows against a real database. Initializes a new fresh sqlite test database for the tests and cleans up the test database when all tests complete successfully.

Async tests using `pytest-asyncio` that exercise complete workflows including I/O operations.

### Experiments (`experiments/`)

The `experiments/` directory (at the project root, sibling to `tests/` and `src/`) holds **learning experiments** — exploratory tests used to validate hypotheses or options, compare old-vs-new code paths, run benchmarks, and validate dependencies. These are **not** part of the automated test suite.

**Purpose:**
- Validate assumptions and hypotheses before they become baked into source code and cause churn
- Compare approaches or dependencies side-by-side (old vs new, library A vs library B)
- Benchmark performance to confirm improvements before committing to a direction
- Evaluate options in a structured, reproducible way so decisions are evidence-based

**How it stays separate from CI:**
- `pyproject.toml` sets `testpaths = ["tests"]`, so `uv run pytest` ignores `experiments/` by default
- Run experiments on demand: `uv run pytest experiments/ -v`
- Run benchmarks: `uv run pytest experiments/ -v -m slow`

**Structure:**
- `experiments/conftest.py` — self-contained fixtures (e.g., DB setup). Duplicates what it needs from `tests/conftest.py` rather than importing from it, keeping experiments decoupled from the test suite.
- `experiments/test_*.py` — experiment files, one test class per hypothesis. Each class typically has:
  - A **correctness test** asserting old and new code paths produce identical results
  - A **benchmark test** (marked `@pytest.mark.slow`) measuring timing improvement

**When to use `experiments/` vs `tests/`:**
- Use `tests/` for regression tests, property tests, and integration tests that should run on every commit
- Use `experiments/` for validating hypotheses, evaluating options, comparing dependencies, and benchmarking — any exploratory work that informs a decision but doesn't need to run in CI

## Property-Based Testing with Hypothesis

### Why Property Tests Over Example-Based

| Example-Based Tests             | Property-Based Tests               |
| ------------------------------- | ---------------------------------- |
| Test specific inputs            | Test input _space_                 |
| May miss edge cases             | Explores edge cases automatically  |
| Documents behavior for one case | Documents invariants for all cases |
| Brittle to refactoring          | Robust to implementation changes   |

Property tests are preferred because they:

1. **Discover edge cases** you didn't think of (unicode, empty strings, boundary values)
2. **Verify invariants** that must hold for all valid inputs
3. **Shrink failures** to minimal reproducible examples
4. **Scale testing** to thousands of cases with one test

### Generators and Strategies

Hypothesis provides strategies for generating test data:

```python
from hypothesis import given
from hypothesis import strategies as st

@given(
    # String matching regex pattern
    name=st.from_regex(r"[a-zA-Z][a-zA-Z0-9_]{0,30}", fullmatch=True),
    # Optional value
    max_tokens=st.none() | st.integers(min_value=1, max_value=10000),
    # Range of values
    temperature=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    # Collection with size bounds
    items=st.lists(st.from_regex(r"[a-z]{1,10}", fullmatch=True), min_size=0, max_size=10),
    # Set (unique values)
    unique_names=st.frozensets(st.from_regex(r"[a-z]{1,10}", fullmatch=True), min_size=0, max_size=5),
)
def test_example_property(name, max_tokens, temperature, items, unique_names):
    """Test body using generated values."""
    assert len(name) <= 31
```

**Common strategies:**

- `st.from_regex(r"[a-zA-Z0-9]{n,m}", fullmatch=True)` - Regex-based string generation
- `st.none() | strategy` - Optional values
- `st.lists(strategy, min_size=n, max_size=m)` - List generation
- `st.frozensets(strategy, min_size=n, max_size=m)` - Unique value sets
- `st.integers(min_value=n, max_value=m)` - Integer ranges
- `st.floats(min_value=n, max_value=m)` - Float ranges
- `st.text(alphabet=..., min_size=n, max_size=m)` - Text generation
- `st.builds(MyClass, field=strategy)` - Build dataclass/object instances
- `st.sampled_from([...])` - Pick from a fixed set of values
- `st.dictionaries(key_strategy, value_strategy)` - Dict generation
- `st.recursive(base, extend)` - Recursive/nested data structures

### Preconditions with assume()

Use `assume()` to filter invalid test cases:

```python
from hypothesis import given, assume
from hypothesis import strategies as st

@given(
    prefix=st.from_regex(r"[a-z]{5,20}", fullmatch=True),
    target=st.from_regex(r"[A-Z]{5,15}", fullmatch=True),
    suffix=st.from_regex(r"[a-z]{5,20}", fullmatch=True),
)
def test_deletion(prefix, target, suffix):
    # Skip cases where prefix/suffix contain target
    assume(target not in prefix)
    assume(target not in suffix)

    # Test proceeds only with valid combinations
```

### Composite Strategies

Use `@st.composite` to build complex custom strategies:

```python
from hypothesis import strategies as st

# Build a composite strategy for a Pydantic extraction model.
# Use st.sampled_from() for enum-like fields, st.none() | strategy for optional fields,
# and st.from_regex() for structured strings like article numbers.

@st.composite
def extraction_items(draw, model_cls, enum_field, enum_values, required_text_field):
    """Generate valid instances of a Pydantic extraction model."""
    enum_val = draw(st.sampled_from(enum_values))
    text_val = draw(st.from_regex(r"[A-Za-z ]{5,80}", fullmatch=True))
    optional_int = draw(st.none() | st.integers(min_value=1, max_value=10))
    optional_ref = draw(st.none() | st.from_regex(r"[1-3]\.\d{1,2}\.[A-Z]", fullmatch=True))
    return model_cls(**{
        enum_field: enum_val,
        required_text_field: text_val,
        "quantity": optional_int,
        "source_ref": optional_ref,
    })

@given(item=extraction_items(
    model_cls=MyModel, enum_field="category", enum_values=["a", "b", "c"],
    required_text_field="description",
))
def test_extraction_item_required_fields_always_present(item):
    """Property: required fields are always populated for all valid inputs."""
    assert item.category in {"a", "b", "c"}
    assert len(item.description) > 0
```

## Key Test Areas

### 1. PDF Ingestion & Section Detection

Tests verify the pipeline correctly extracts content from specification PDFs:

- File hashing and deduplication (same PDF not re-ingested)
- Page text extraction completeness
- Section boundary detection (CSI MasterFormat section numbers)
- Division/section number parsing and ordering

### 2. Chunking & Page Markers

Tests for hierarchical chunking of spec sections:

- **Token counting**: Chunks stay within `CHUNK_TARGET_TOKENS` / `CHUNK_MAX_TOKENS` bounds
- **Content hashing**: Identical text produces identical hashes
- **Hierarchy paths**: Division/section/part/article paths are well-formed
- **Page marker preservation**: `<<PAGE:NNN>>` markers survive chunking
- **Article/part boundaries**: Chunks don't cross structural boundaries

**Property test opportunities:**

```python
from hypothesis import given
from hypothesis import strategies as st

@given(
    text=st.text(min_size=1, max_size=5000, alphabet=st.characters(categories=("L", "N", "P", "Z"))),
)
def test_chunk_token_count_bounded(text):
    """Property: Every chunk's token count is within configured bounds."""
    chunks = chunk_text(text, target=1000, maximum=1200)
    for chunk in chunks:
        assert count_tokens(chunk.text) <= 1200
```

### 3. Serialization Roundtrips

Tests verify Pydantic model serialization/deserialization consistency:

```python
from hypothesis import given
from hypothesis import strategies as st

# Use a composite strategy (see above) or build inline strategies for each field.
# The key invariant: model_dump_json → model_validate_json is identity.

@given(
    category=st.sampled_from(["type_a", "type_b", "type_c"]),
    description=st.from_regex(r"[A-Za-z ]{5,80}", fullmatch=True),
    quantity=st.none() | st.integers(min_value=1, max_value=100),
)
def test_serialization_roundtrip_preserves_data(category, description, quantity):
    """Property: serialize -> deserialize is identity for all valid inputs."""
    item = MyExtractionModel(category=category, description=description, quantity=quantity)
    json_str = item.model_dump_json()
    deserialized = MyExtractionModel.model_validate_json(json_str)

    assert item == deserialized
```

### 4. LLM Structured Extraction

Tests for Anthropic tool_use based extraction:

- `call_llm` returns parsed tool input from tool_use blocks
- Raises `ValueError` when no tool_use block in response
- Correct model/temperature/tool_choice passed to API
- Empty or None part text gracefully returns empty results
- Each defined extraction type correctly parses its response schema

### 5. Knowledge Graph Construction

Tests for entity promotion, relationship inference, and deduplication:

- **Entity promotion**: Extracted items promoted to knowledge graph entities
- **Global deduplication**: Same entity referenced across multiple sections produces one graph node
- **Relationship inference**: All defined relationship types create correct edges between entity types
- **Idempotency**: Running graph construction twice produces no duplicates
- **Graph validation**: Zero dangling relationships, orphan detection
- **Normalization**: Case and whitespace variants resolve to single entity

### 6. Error Handling & Consecutive Failures

Tests verify extraction error handling:

- Authentication errors (401, missing API key) abort immediately
- Consecutive failures beyond `MAX_CONSECUTIVE_FAILURES` abort the extraction type
- Successful extraction resets the consecutive failure counter
- Dry-run mode processes without API calls

## Test Documentation Requirements

**Every test MUST document:**

1. **Purpose**: Why this test is important
2. **Invariant**: What property/behavior it verifies
3. **Context**: When this matters (failure scenarios, edge cases)

### Required Format

```python
def test_example():
    """Test Name: Brief description of what's being tested.

    Why this is important: Explain the significance and potential
    failure modes this test catches. Include real-world scenarios.

    Invariant: Formal statement of the property being verified.
    Use mathematical notation if helpful (e.g., "for all inputs: P(x) -> Q(x)")
    """
    ...
```

### Example

```python
@given(
    entity_type=st.sampled_from(["product", "material", "standard", "manufacturer"]),
    name=st.text(
        alphabet=st.characters(categories=("L", "N", "Z", "P")),
        min_size=1, max_size=80,
    ),
)
def test_entity_normalization_idempotent(entity_type, name):
    """Property: Normalizing an entity name twice produces the same result.

    This property verifies the normalization idempotency invariant:
    - normalize(normalize(name)) == normalize(name) for all valid inputs
    - Prevents deduplication drift where re-processing creates new entities

    Critical for knowledge graph integrity — the same entity referenced
    with different casing or whitespace across sections must resolve
    consistently to a single graph node.
    """
    once = normalize_entity_name(entity_type, name)
    twice = normalize_entity_name(entity_type, once)

    assert once == twice, (
        f"normalization is not idempotent: {name!r} -> {once!r} -> {twice!r}"
    )
```

## Mock Implementations

### Mock Anthropic Client for Extraction Tests

Used to simulate LLM tool_use responses without network calls:

```python
from unittest.mock import MagicMock
import anthropic

def _mock_llm_response(data: dict) -> MagicMock:
    """Create a mock Anthropic client returning a tool_use response with given data."""
    mock_client = MagicMock(spec=anthropic.Anthropic)
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "extract"
    tool_block.input = data
    response = MagicMock()
    response.content = [tool_block]
    mock_client.messages.create.return_value = response
    return mock_client

# Usage — pass whatever extraction schema the LLM would return:
client = _mock_llm_response({
    "items": [
        {"name": "Example Item", "category": "type_a", "source_article": "1.3.A"}
    ]
})
```

### Test Database Setup

Each test file defines a `_create_test_db()` helper that creates a minimal SQLite database
with schema and sample data:

```python
def _create_test_db(db_path: Path) -> sqlite3.Connection:
    """Create a test database with schema and sample sections."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (...);
        CREATE TABLE IF NOT EXISTS spec_sections (...);
        CREATE TABLE IF NOT EXISTS submittals (...);
        -- ... remaining extraction and graph tables
    """)
    # Insert sample document and sections
    conn.execute("INSERT INTO documents ...")
    conn.execute("INSERT INTO spec_sections ...")
    conn.commit()
    return conn
```

### Embedding Model Mock

Used to test embedding storage and KNN queries without loading a real model:

```python
import numpy as np
from unittest.mock import MagicMock

mock_model = MagicMock()
mock_model.encode.return_value = np.random.randn(batch_size, 768).astype(np.float32)
```

## Async Testing

> **Note:** The current test suite is synchronous. This section is reference for when async
> patterns are needed (e.g., async LLM clients, concurrent pipeline phases).

### pytest-asyncio

For async tests, use the `@pytest.mark.asyncio` decorator:

```python
import pytest

@pytest.mark.asyncio
async def test_embedding_batch(tmp_path):
    db_path = tmp_path / "test.db"
    conn = _create_test_db(db_path)
    # ... test async embedding operations
    assert result_count > 0
```

### Async Property Tests

Hypothesis supports async tests natively with pytest-asyncio:

```python
from hypothesis import given
from hypothesis import strategies as st
import pytest

@pytest.mark.asyncio
@given(query=st.from_regex(r"[a-z ]{5,50}", fullmatch=True))
async def test_search_always_returns_list(query):
    results = await async_search(query)
    assert isinstance(results, list)
```

## Running Tests

### All Tests

```bash
pytest
```

### Specific Module

```bash
pytest tests/test_extract_knowledge.py
pytest tests/test_build_graph.py
pytest tests/test_chunking.py
```

### Test Filtering

```bash
# Run tests matching pattern
pytest -k normalize

# Run specific test
pytest tests/test_build_graph.py::TestNormalizeEntityName::test_material_title_case

# Run tests in specific class
pytest tests/test_extract_knowledge.py::TestCallLlm

# Run with output displayed
pytest -s

# Run only failed tests from last run
pytest --lf

# Run marked tests
pytest -m "slow"
```

### Hypothesis Configuration

Control Hypothesis behavior via settings or profiles:

```python
# In conftest.py
from hypothesis import settings, Phase

# Register profiles
settings.register_profile("ci", max_examples=1000, deadline=None)
settings.register_profile("dev", max_examples=50, deadline=500)
settings.register_profile("debug", max_examples=10, deadline=None, phases=[Phase.explicit, Phase.generate])

# Load via HYPOTHESIS_PROFILE env var or:
settings.load_profile("dev")
```

```bash
# Run with CI profile (more examples)
HYPOTHESIS_PROFILE=ci pytest

# Set seed for reproducibility
HYPOTHESIS_SEED=12345 pytest

# Show Hypothesis statistics
pytest --hypothesis-show-statistics
```

Per-test settings override:

```python
from hypothesis import given, settings
from hypothesis import strategies as st

@settings(max_examples=500, deadline=None)
@given(name=st.text(min_size=1, max_size=100))
def test_thorough_property(name):
    ...
```

## Test Helpers

### Workspace Setup

Use `tmp_path` fixture (pytest built-in) or `tempfile` for isolated filesystem tests:

```python
import pytest

def test_with_workspace(tmp_path):
    """tmp_path provides a unique temporary directory per test."""
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello")
    assert test_file.read_text() == "hello"
```

### Test Database & Entity Setup

Helper functions for creating test databases with extraction and graph data.
Each test file typically defines these three layers:

```python
def _create_test_db(db_path: Path) -> sqlite3.Connection:
    """Create a minimal test database with schema and sample sections."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""...""")  # Full schema
    # Insert sample document + sections
    return conn

def _populate_extraction_data(conn: sqlite3.Connection) -> None:
    """Add sample extraction rows (one per extraction table)."""
    # Insert representative rows into each extraction table
    conn.commit()

def _setup_entities(conn: sqlite3.Connection) -> None:
    """Promote all entity types so relationship inference tests can run."""
    _populate_extraction_data(conn)
    # Call each promote_* function to build the entity graph
    conn.commit()
```

### LLM Response Builders

Helpers for constructing mock Anthropic tool_use responses:

```python
def _mock_llm_response(data: dict) -> MagicMock:
    """Create a mock client that returns a tool_use response with the given data."""
    mock_client = MagicMock(spec=anthropic.Anthropic)
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "extract"
    tool_block.input = data
    response = MagicMock()
    response.content = [tool_block]
    mock_client.messages.create.return_value = response
    return mock_client
```

## Test Dependencies

Add to `pyproject.toml` under test dependencies:

```toml
[project.optional-dependencies]
dev = [
    "mypy>=1.10",
    "pytest>=8.0",
    "ruff>=0.4",
]
```

When adding property-based or async tests, add these to the dev dependencies:

```toml
dev = [
    "hypothesis>=6.100",
    "mypy>=1.10",
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.4",
]
```
