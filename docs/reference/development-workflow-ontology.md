# Development Workflow Ontology

A comprehensive taxonomy of all LLM-assisted development workflows observed across 459 sessions in 50+ projects. Derived from empirical analysis of Claude Code chat history across all `~/.claude/projects/` sessions.

This ontology serves as a reference for selecting the right workflow for a given task, and as a foundation for automating workflow selection within the build-your-room orchestration pipeline.

---

## Category A: Autonomous Batch Production

Zero human interaction during execution. A queue or SDK dispatches identical templated prompts. The agent self-selects work from a shared artifact (spec, plan, or task list).

### A1. Spec-Driven Task Queue

**Description:** Queue fires "implement next incomplete task" against a spec or plan document. Agent reads the task list, picks the next available task, implements code and tests, runs lint/typecheck/tests, writes a diary entry, and commits.

**When to use:** Building an entire system from a specification. High-throughput, parallel-safe when tasks are independent.

**Input artifact:** Spec document + task list
**Output artifact:** Code, tests, diary entry, commit
**Human involvement:** None during execution; human writes spec beforehand
**Automation level:** Fully autonomous
**Session multiplicity:** Parallel batch (many agents, same prompt)

**Observed in:** build-the-room (~60 sessions), clean-your-room (~21 sessions), deep-research-cli (~51 sessions)

**Prompt template example:**
> "Study @build-your-room-spec.md thoroughly. Use HTN planning and implement the next incomplete task ready to be implemented from the task list."

**Known issues:**
- Parallel race condition: multiple agents may independently pick the same "next" task, producing duplicate work.
- Context overflow on complex tasks (e.g., ValidationStage, ClaudeAgentAdapter) requiring auto-continuation.

---

### A2. Spec Factory: Creation

**Description:** "Identify one specification that still needs created and create it." Agent inventories existing specs, identifies uncovered subsystems in source code, and writes a new clean-room behavioral specification.

**When to use:** Generating specs for a codebase you want to reimplement from scratch. The clean-room constraint ensures specs describe behavioral contracts without referencing implementation details.

**Input artifact:** Existing source code + existing spec inventory
**Output artifact:** New specification file
**Human involvement:** None during execution
**Automation level:** Fully autonomous
**Session multiplicity:** Parallel batch

**Observed in:** excalidraw (~20 sessions), twenty (~11 sessions)

**Prompt template example:**
> "Study the existing specs/* -- Identify one specification that still needs created for the clean room deep research specifications and create the specification file. -- Important: Describe behavioral contracts and constraints, not implementation. Do not reference variable names, function names, file paths, migration IDs, or internal state fields from the source."

---

### A3. Spec Factory: Improvement

**Description:** "Identify one spec and improve it." Agent selects the weakest existing specification and enriches it with provable properties catalog, purity boundary map, verification tooling selection, and mermaid diagrams.

**When to use:** Iteratively strengthening a corpus of specifications. Each pass targets the weakest spec, raising the quality floor over time.

**Input artifact:** Existing specs + source code
**Output artifact:** Improved specification file
**Human involvement:** None during execution
**Automation level:** Fully autonomous
**Session multiplicity:** Parallel batch

**Observed in:** erxes (~12 sessions), pipe-dream (~10 sessions), twenty (~20 sessions), onyx (~51 sessions)

**Prompt template example:**
> "Study the existing specs/* -- Identify one specification for the clean room deep research specifications and improve the specification file. Persist changes when done. -- Focus on ONE specification -- Include: Provable Properties Catalog, Purity Boundary Map, Verification Tooling Selection, Property Specifications"

**Known issues:**
- Multiple parallel agents sometimes select the same "weakest" spec, producing duplicate improvement work.

---

### A4. AI-Generated Issue Dispatch

**Description:** A meta-orchestrator session asks the AI to audit the codebase and identify issues (security, performance, error handling, code quality). It then spawns individual micro-task sessions with structured prompts. Each dispatched session gets a specific category, file path, and fix description.

**When to use:** When you want the AI to find its own work to do. Useful for codebase-wide sweeps (security hardening, error handling, cleanup).

**Input artifact:** Codebase (agent discovers issues autonomously)
**Output artifact:** Many targeted fixes across the codebase
**Human involvement:** Human initiates meta-session; micro-tasks run autonomously
**Automation level:** Fully autonomous (two-tier)
**Session multiplicity:** 1 orchestrator + N parallel micro-tasks

**Observed in:** manado conductor workspace (~10 sessions)

**Prompt template example (orchestrator):**
> "Think up good issues and launch a bunch of agents in tmux and supervise them"

**Prompt template example (dispatched micro-task):**
> "Fix security issue: Add UUID validation to /app/api/chat/route.ts..."

---

## Category B: Orchestrator-Managed Development

An external system (e.g., Conductor) manages session lifecycle via `system_instruction`. Human supervises the orchestration layer but does not interact with individual sessions.

### B1. Conductor-Orchestrated Implementation

**Description:** Conductor injects a system instruction with workspace/branch isolation context. The agent implements within those guardrails. Often dispatches subagents for parallel research (component analysis, schema exploration, architecture mapping) before proceeding with implementation.

**When to use:** Running many parallel agents on isolated branches with workspace-level isolation. Good for sustained multi-day feature development with automatic branch management.

**Input artifact:** System instruction with task context
**Output artifact:** Code changes on isolated branch
**Human involvement:** Supervisory (manages Conductor, not individual sessions)
**Automation level:** Semi-autonomous
**Session multiplicity:** Many parallel sessions per workspace

**Observed in:** charlotte (~25 sessions), havana (~12), los-angeles (~6), san-jose (~7), worcester (~1), tel-aviv (~1), istanbul (~18)

**Key characteristics:**
- 1:1 workspace-to-branch mapping ensures strict isolation.
- Work happens in intensive bursts (e.g., 18 sessions in a 3.5-hour window).
- Subagent research is nearly universal before implementation begins.
- Prompt suggestion subagents may predict user's next input.

---

## Category C: Plan-Driven Development

Human creates or approves a plan document, then executes it across one or more sessions. Clear separation between "think" and "do" phases.

### C1. Brainstorming / Design

**Description:** Interactive feature design via `/superpowers:brainstorming` or conversational exploration. The human and AI collaboratively explore requirements, constraints, trade-offs, and design options. Produces a plan document or design decision.

**When to use:** When you need to figure out *what* to build before *how* to build it. First step in the plan-driven development cycle.

**Input artifact:** Feature idea, user feedback, or problem statement
**Output artifact:** Plan document, design decision, or spec
**Human involvement:** Active collaboration (pushback, correction, refinement)
**Automation level:** Fully interactive
**Session multiplicity:** Single session (may spawn follow-up plan creation)

**Observed in:** pipe-dream (~25 sessions)

**Key characteristics:**
- User actively pushes back on AI proposals ("retry" vs "reprocess", rejecting visual approaches).
- Often references experiment results, ground truth data, or competitor tools (BlueBeam).
- May include screenshots of desired behavior from other tools.

---

### C2. Plan Creation

**Description:** Structured plan authoring via `/cl:create_plan` or interactive discussion. User provides constraints and context, agent drafts a detailed implementation plan with task breakdown, dependencies, and design decisions.

**When to use:** When brainstorming has produced a clear direction and you need a concrete implementation plan before execution.

**Input artifact:** Brainstorming output, research documents, design decisions
**Output artifact:** Committed plan `.md` file with task breakdown
**Human involvement:** Active (provides constraints, approves plan)
**Automation level:** Interactive
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~5 sessions), wizard workspaces

---

### C3. Plan Review / Revision

**Description:** Reviewing an existing plan against new findings, critical discoveries, or changed requirements. Revises the plan before execution begins or continues.

**When to use:** When new information emerges that affects an existing plan (e.g., "critical findings about drag preview mutations in annotation editing plan").

**Input artifact:** Existing plan + new findings
**Output artifact:** Revised plan
**Human involvement:** Active
**Automation level:** Interactive
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~2 sessions)

---

### C4. Plan Execution (Skill-Driven)

**Description:** `/superpowers:executing-plans` or `/cl:implement_plan` executes a pre-written plan file. Agent follows plan steps, creates HTN task breakdown, may dispatch subagents for parallelizable work, runs tests, and commits.

**When to use:** When you have a written, reviewed plan and want to execute it. The most common path from plan to code.

**Input artifact:** Plan `.md` file
**Output artifact:** Implemented code, tests, commits
**Human involvement:** Minimal (agent follows plan autonomously)
**Automation level:** Semi-autonomous (human may intervene on failures)
**Session multiplicity:** Single session (may require retry/continuation)

**Observed in:** pipe-dream (~23 sessions), wizard workspaces (~5 sessions)

**Key characteristics:**
- Some sessions use dev browser for self-testing changes.
- Complex plans may require multiple sessions (retry or continuation).
- Agent warns about coordination with other agents when working on shared code.

---

### C5. Plan-Driven Serial Task Execution

**Description:** Human repeatedly fires the same prompt: "Study plan, implement next incomplete task." Each session processes one task from the plan, and the human re-fires the prompt for the next. Creates a manual task queue.

**When to use:** When tasks have dependencies that prevent full parallelization, or when you want human checkpoint between each task.

**Input artifact:** Plan document with task list
**Output artifact:** One implemented task per session
**Human involvement:** Re-fires prompt between sessions; may dispatch subagents for parallelizable subtasks
**Automation level:** Human-paced automation
**Session multiplicity:** Serial chain (same prompt, many sessions)

**Observed in:** kenmore-cv-istanbul (~10 sessions)

**Prompt template example:**
> "Study ...plan.md thoroughly. Implement the next incomplete task... If tasks can be run in parallel, then run them as subagents."

---

### C6. HTN-Driven Gap Closure

**Description:** Study a gap analysis document, use HTN planning to determine the highest-value next task, implement it, update task status, repeat. An iterative loop that closes gaps against ground truth or a reference standard.

**When to use:** When you have a known gap between current state and desired state (e.g., ground truth validation results) and want to systematically close it.

**Input artifact:** Gap analysis document + ground truth data
**Output artifact:** Implementations that close identified gaps
**Human involvement:** Re-fires prompt between iterations
**Automation level:** Human-paced automation
**Session multiplicity:** Serial chain

**Observed in:** pipe-dream-ground-truth (~5 sessions)

**Prompt template example:**
> "Study the gaps from docs/p14-ground-truth-analysis.md. Use HTN planning to determine which task to do next."

---

## Category D: Interactive Feature & Bug Work

Human-in-the-loop, conversational, often multi-turn. Human provides input, evaluates output, and provides feedback.

### D1. Direct Feature Request

**Description:** "Add tooltip on hover", "Enable fill by default." User describes desired behavior in 1-2 sentences. Agent implements with minimal back-and-forth. Small, well-scoped changes.

**When to use:** Small, well-understood changes where the intent is clear and no design exploration is needed.

**Input artifact:** Short verbal description
**Output artifact:** Code change + tests
**Human involvement:** Minimal (request and verify)
**Automation level:** Interactive but low-touch
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~14 sessions)

---

### D2. Screenshot-Driven Bug Fix

**Description:** User provides a screenshot plus symptom description. The AI is expected to perform root cause analysis *before* proposing a fix. After changes, iterative visual verification via new screenshots.

**When to use:** Visual/UI bugs where the symptom is best communicated as an image.

**Input artifact:** Screenshot + symptom description
**Output artifact:** Bug fix with root cause explanation
**Human involvement:** Active (provides screenshots, evaluates visual results)
**Automation level:** Interactive
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~10 sessions: annotations, callouts, scaling, blurriness)

**Key characteristics:**
- User expects root cause analysis before any code changes.
- Multiple visual verification cycles per session.
- Screenshots from the running application are the primary evidence.

---

### D3. Error-Log Bug Debugging

**Description:** User pastes error output, stack traces, or test failures. Systematic diagnosis follows, often via `/superpowers:systematic-debugging`. May span multiple sessions for recurring or persistent bugs.

**When to use:** Runtime errors, test failures, or crashes where the error output is available.

**Input artifact:** Error logs, stack traces
**Output artifact:** Bug fix with diagnosis
**Human involvement:** Active (provides logs, confirms fix)
**Automation level:** Interactive
**Session multiplicity:** May span multiple sessions for recurring bugs

**Observed in:** deep-research-cli (~10 sessions)

---

### D4. Iterative UI Refinement

**Description:** Extended back-and-forth on visual/UX adjustments. Human evaluates visually, provides qualitative feedback ("too blurry", "need to see behind the highlighted coupling"), agent adjusts. Multiple refinement cycles per session.

**When to use:** Tuning visual presentation, layout, transparency, or interaction feel. Subjective quality that requires human judgment.

**Input artifact:** Running UI + verbal feedback
**Output artifact:** Refined UI code
**Human involvement:** Active throughout (evaluate, feedback, iterate)
**Automation level:** Fully interactive
**Session multiplicity:** Single session (extended)

**Observed in:** pipe-dream (~5 sessions: coupling transparency, annotation scaling)

---

### D5. Bug Triage from Memory

**Description:** "Are there any outstanding bugs in your memory?" Agent checks persistent memory for known issues, then explores with dev browser to verify or triage them.

**When to use:** Periodic housekeeping to address accumulated known issues.

**Input artifact:** Agent memory
**Output artifact:** Bug fixes or triage decisions
**Human involvement:** Initiates, then observes
**Automation level:** Semi-autonomous
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~1 session)

---

## Category E: Research & Understanding

Goal is knowledge acquisition, not code changes. Output is understanding, specifications, documented findings, or experiment results.

### E1. Deep Codebase Research

**Description:** Structured research queries with numbered questions targeting specific files, components, or subsystems. Via `/cl:research_codebase` or detailed manual prompts. Often used as pre-implementation reconnaissance.

**When to use:** Before implementing a feature that touches unfamiliar code. Understanding existing patterns, data models, interaction handlers, or state management before making changes.

**Input artifact:** Research questions targeting specific files/features
**Output artifact:** Documented understanding, research notes
**Human involvement:** Directs research focus
**Automation level:** Semi-autonomous
**Session multiplicity:** Often a series (e.g., 5 research sessions → implementation)

**Observed in:** pipe-dream-pipe-id (~5 sessions), pipe-dream codebase research (~5 sessions)

**Key characteristics:**
- Research sessions are often serialized: each targets a specific file, building up a complete picture.
- Research output feeds directly into plan creation sessions.

---

### E2. Learning Experiment

**Description:** Formal experiment framework (`/rw:learning-experiment`) to test detection, classification, or ML approaches before committing to an implementation path. Hypothesis-driven, with parallel experiments and comparative analysis.

**When to use:** When the right technical approach is uncertain and you need to evaluate multiple strategies empirically (e.g., fitting detection via geometry characterization vs. similarity matching).

**Input artifact:** Hypothesis, test data (e.g., P-14/P-15 drawings)
**Output artifact:** Experiment results, approach recommendation
**Human involvement:** Designs experiment, evaluates results
**Automation level:** Semi-autonomous
**Session multiplicity:** Often parallel (multiple experiments simultaneously)

**Observed in:** pipe-dream (~7 sessions: fittings, couplings, symbols, walls, embeddings)

---

### E3. Parallel Approach Exploration

**Description:** Multiple sessions launched simultaneously, each exploring the same problem with a different methodology. Competitive evaluation to determine which approach best captures the problem's properties.

**When to use:** When multiple fundamentally different approaches could work and you want to evaluate them side-by-side without sequential bias.

**Input artifact:** Problem statement (same for all sessions)
**Output artifact:** Multiple approach evaluations for comparison
**Human involvement:** Launches sessions, compares results
**Automation level:** Fully autonomous per session
**Session multiplicity:** Parallel (one session per approach)

**Observed in:** los-angeles conductor workspace (~5 sessions: Alloy vs Spin vs B/Event-B vs PRISM vs TLA+)

---

### E4. Quality / Purity Audit

**Description:** Verifying spec independence, completeness, or correctness. "Are these specs truly clean room?" Read-only critical assessment of generated artifacts.

**When to use:** After batch spec generation to verify quality, independence from source, and completeness before using specs for reimplementation.

**Input artifact:** Generated specs
**Output artifact:** Audit findings, quality assessment
**Human involvement:** Initiates and reviews findings
**Automation level:** Interactive
**Session multiplicity:** Multiple sessions (different audit angles)

**Observed in:** onyx (~5 sessions)

---

### E5. Architecture Review

**Description:** "Make recommendations for hexagonal architecture. Make no changes." Read-only architectural assessment with explicit no-modification constraint.

**When to use:** When you want an outside perspective on architecture without risk of unintended changes.

**Input artifact:** Codebase
**Output artifact:** Architectural recommendations
**Human involvement:** Reads recommendations
**Automation level:** Interactive (read-only)
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~1 session)

---

### E6. Knowledge Alignment

**Description:** Comparing system behavior against an external reference document (customer SOP, interview transcript, competitor tool). Gap analysis between what the system does and what stakeholders expect.

**When to use:** When you have an external reference standard (customer requirements, SOP, interview notes) and want to identify gaps.

**Input artifact:** External reference document + current system state
**Output artifact:** Gap analysis
**Human involvement:** Provides reference, reviews gaps
**Automation level:** Interactive
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~1 session: taxonomy vs customer SOP/interview)

---

### E7. Meta-Analysis

**Description:** Cross-session or cross-project analysis of development patterns, workflows, or process. Research about the development process itself rather than the product.

**When to use:** Process improvement, workflow documentation, retrospective analysis.

**Input artifact:** Session history across projects
**Output artifact:** Workflow ontology, process documentation
**Human involvement:** Directs analysis
**Automation level:** Interactive with parallel subagents
**Session multiplicity:** Single session

**Observed in:** build-the-room (~1 session: this analysis)

---

## Category F: Operations & Maintenance

Supporting development infrastructure. Not producing features or specifications.

### F1. VCS Operations

**Description:** Executing version control commands: rebasing, merging, committing, PR creation, workspace management. "Push latest to main", "rebase on upstream changes."

**When to use:** Routine version control operations where the commands are known but tedious to type.

**Input artifact:** Verbal instruction
**Output artifact:** VCS state change
**Human involvement:** Directs operation
**Automation level:** Interactive
**Session multiplicity:** Single session (quick)

**Observed in:** pipe-dream (~10 sessions), clean-your-room (~5 sessions)

---

### F2. VCS Learning / Help

**Description:** Conceptual questions about version control tools. "What's the difference between jj workspace and git worktree?" Learning the tool rather than executing a known operation.

**When to use:** When learning a new VCS tool (especially jj/Jujutsu) or encountering unfamiliar VCS concepts.

**Input artifact:** Question
**Output artifact:** Explanation
**Human involvement:** Active (asks follow-ups)
**Automation level:** Fully interactive
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~5 sessions)

---

### F3. CLI / Tooling Enhancement

**Description:** Improving developer-facing tooling: adding port configuration to CLI, workspace commands, cross-project agent setup.

**When to use:** When developer tools need new capabilities to support the workflow.

**Input artifact:** Feature request for tooling
**Output artifact:** Tooling code changes
**Human involvement:** Specifies requirements
**Automation level:** Interactive
**Session multiplicity:** Single session

**Observed in:** pipe-dream (~5 sessions), kenmore-experiments

---

### F4. Configuration / Setup

**Description:** Model settings, plugin management, .env file support, installation help, onboarding to new tools.

**When to use:** Initial project setup or environment configuration changes.

**Input artifact:** Setup requirements or questions
**Output artifact:** Configuration changes or instructions
**Human involvement:** Active
**Automation level:** Interactive
**Session multiplicity:** Single session

**Observed in:** deep-research-cli, various projects (~5 sessions)

---

### F5. Knowledge Management

**Description:** PARA method inbox processing, weekly synthesis, ubiquitous language glossary creation. Organizational and documentation work outside of application code.

**When to use:** Periodic knowledge organization, domain modeling documentation.

**Input artifact:** Inbox items, session history, domain concepts
**Output artifact:** Organized knowledge artifacts, glossary
**Human involvement:** Initiates
**Automation level:** Semi-autonomous
**Session multiplicity:** Single session (recurring)

**Observed in:** knowledge-repository (~3 sessions), pipe-dream (~1 session)

---

### F6. Ad-hoc Tool Usage

**Description:** One-off utility tasks not directly related to software development: ffmpeg file splitting, security dependency scanning, file cleanup.

**When to use:** When you need a quick utility task done and the AI is the fastest way to get the right command.

**Input artifact:** Problem description
**Output artifact:** Executed command or script
**Human involvement:** Active (cautious, "do not take action until I'm clear on the plan")
**Automation level:** Interactive
**Session multiplicity:** Single session

**Observed in:** Volumes-camera (~1 session), pipe-dream security scan (~1 session)

---

## Category G: Runtime / Cross-Cutting Patterns

Not workflow choices but emergent behaviors that occur within other workflows.

### G1. Context-Overflow Continuation

**Description:** Session exceeds the context window and auto-continues with a compacted summary of prior work. The continuation picks up where the original left off.

**Trigger:** Complex tasks that exceed a single context window (e.g., ValidationStage, ClaudeAgentAdapter, large spec improvements).
**Observed in:** build-the-room (~3 sessions), conductor (~1 session), clean-room-specs (~3 sessions)

---

### G2. Parallel Race Condition

**Description:** Multiple queued sessions independently select the same "next" task from a shared task list. One wins the commit; the other produces potentially useful alternative implementations or additional tests.

**Trigger:** Parallel batch execution (Category A) with shared mutable task list.
**Observed in:** build-the-room (~6 pairs, ~12 sessions)

---

### G3. Instrumentation Probe

**Description:** Deliberately minimal sessions ("Read test-file.txt") generating telemetry for testing an observability system. Not real development work -- synthetic test data.

**Trigger:** Building/testing agent observability tools.
**Observed in:** san-jose-experiments (~2 sessions)

---

### G4. Feature Ideation

**Description:** "I'm looking for a new feature to add which is useful and doesn't have a bunch of work to setup." Agent proposes candidate features based on codebase analysis.

**When to use:** When you want AI-suggested features for demo, exploration, or backlog building.

**Input artifact:** Codebase + criteria (demo-friendly, low setup)
**Output artifact:** Feature proposals
**Human involvement:** Evaluates proposals
**Automation level:** Interactive
**Session multiplicity:** Single session

**Observed in:** clean-your-room (~2 sessions)

---

## Workflow Selection Decision Tree

```
START: What do you need to do?
|
+-- "Build a whole system from spec"
|   +-- Spec exists? --> A1. Spec-Driven Task Queue
|   +-- Need specs first? --> A2/A3. Spec Factory --> then A1
|
+-- "Implement a planned feature"
|   +-- Have a plan? --> C4. Plan Execution (Skill-Driven)
|   +-- Need a plan?
|       +-- Know what to build? --> C2. Plan Creation
|       +-- Need to explore options? --> C1. Brainstorming
|       +-- Need to understand existing code first? --> E1. Deep Codebase Research
|
+-- "Fix a bug"
|   +-- Have a screenshot? --> D2. Screenshot-Driven Bug Fix
|   +-- Have error logs? --> D3. Error-Log Bug Debugging
|   +-- Know the symptom? --> D1. Direct Feature Request (as bug fix)
|
+-- "Evaluate approaches"
|   +-- Same problem, different methods? --> E3. Parallel Approach Exploration
|   +-- Need empirical data? --> E2. Learning Experiment
|
+-- "Close gaps against a reference"
|   +-- Have gap analysis? --> C6. HTN-Driven Gap Closure
|   +-- Need gap analysis? --> E6. Knowledge Alignment
|
+-- "Improve quality at scale"
|   +-- Find issues AI can fix? --> A4. AI-Generated Issue Dispatch
|   +-- Verify generated artifacts? --> E4. Quality Audit
|
+-- "Tune visual/UX details"
|   --> D4. Iterative UI Refinement
|
+-- "Manage branches/commits"
|   --> F1. VCS Operations
```

---

## Cross-Cutting Dimensions

Every workflow can be characterized along these independent axes:

| Dimension | Values |
|-----------|--------|
| **Automation level** | Fully autonomous / Orchestrator-managed / Human-initiated-then-autonomous / Fully interactive |
| **Session multiplicity** | Single session / Serial chain / Parallel batch |
| **Input artifact** | Spec doc / Plan doc / Screenshot / Error log / Verbal description / Gap analysis / None |
| **Output artifact** | Code+tests / Spec doc / Plan doc / Knowledge / Config change / VCS state |
| **Human involvement during execution** | None / Supervisory / Active collaboration |
| **Phase in development lifecycle** | Research / Design / Plan / Implement / Test / Debug / Review / Ship / Maintain |

---

## Methodology

This ontology was derived on 2026-04-10 by analyzing 459 chat sessions across 50+ projects in `~/.claude/projects/`. Five parallel research agents examined session data grouped by project family:

1. **build-the-room** (61 sessions): Autonomous spec-driven task queue
2. **clean-room-specs-monorepo** (74 sessions): Batch spec creation and improvement
3. **conductor-workspaces** (84 sessions): Orchestrator-managed development
4. **pipe-dream / kenmore** (142 sessions): Full lifecycle -- research, brainstorm, plan, implement, debug
5. **Miscellaneous projects** (159 sessions): HTN task execution, debugging, knowledge management, tooling

Every session was categorized by its initial prompt, interaction pattern, automation level, and output type. Patterns observed even once are included to ensure collective exhaustiveness.
