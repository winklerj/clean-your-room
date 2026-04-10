# Task 8: Config module with all env vars

## What was done

Extended `config.py` from a flat env-var listing to a complete configuration module:

1. **Added missing env vars**: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEVBROWSER_SKILL_PATH` — all from the spec's environment variables table.

2. **Added `PipelineConfig` frozen dataclass**: Typed representation of the per-pipeline `config_json` runtime overrides. All 12 fields match the spec's runtime config schema with sensible defaults. Includes:
   - `from_json()` — parses stored JSON string or dict, silently ignoring unknown keys for forward compatibility
   - `to_json()` — serializes for DB storage
   - `merge()` — returns a new config with overrides applied (unknown keys dropped)

3. **Validation**: `__post_init__` validates `context_threshold_pct` (0-100), `max_concurrent_stages` (>= 1), and `lease_ttl_sec` (>= 1). Raises `ConfigError(ValueError)` on invalid values.

4. **Literal types**: `PropertyTestFramework`, `RemotePublishPolicy`, `RotationPolicy` — typed string unions matching spec options.

## Learnings

- Frozen dataclasses work well for config objects — immutability prevents accidental mutation mid-pipeline, and `from_json`/`merge` return new instances.
- Using `fields(cls)` for both serialization and unknown-key filtering keeps the code DRY and automatically adapts when new fields are added.
- Forward-compatible JSON parsing (silently dropping unknown keys) is important since stored `config_json` may contain fields from newer versions.

## Test count

42 new tests (7 property-based, 35 unit). Total: 271 tests, all passing.
