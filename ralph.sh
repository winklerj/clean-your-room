#!/bin/bash

# Ralph Wiggum - Iterative Implementation Script
# "I'm helping!" - Ralph Wiggum
#
# Usage: ./ralph.sh [plan-file] [prompt-file] [max-iterations]
#
# Defaults:
#   plan-file:      SPECIFICATION.md
#   prompt-file:    PROMPT.md (uses built-in default if not found)
#   max-iterations: 20
#
# This script iteratively runs Claude to implement items from a plan file,
# one item at a time, until completion or max iterations reached.
#
# The prompt file can use ${PLAN_FILE} which will be substituted.

set -e

PLAN_FILE="${1:-SPECIFICATION.md}"
PROMPT_FILE="${2:-PROMPT.md}"
MAX_ITERATIONS="${3:-20}"
ITERATION=1
SLEEP_SECONDS=20

# Default prompt used when no prompt file exists
DEFAULT_PROMPT='Study @${PLAN_FILE} thoroughly.

Implement the next incomplete item in the plan.

Important:
- Write property-based tests for any code you implement
- Lint, run tests and type checking after making changes until all pass
- Update the plan with progress on the item
- Git commit the changes (no Claude attribution), push if remote exists

Focus on ONE item per iteration. Make the code production-ready.'

if [ ! -f "$PLAN_FILE" ]; then
    echo "Error: Plan file not found: $PLAN_FILE"
    exit 1
fi

# Load prompt from file or use default
if [ -f "$PROMPT_FILE" ]; then
    PROMPT_TEMPLATE=$(cat "$PROMPT_FILE")
    echo "Using prompt file: $PROMPT_FILE"
else
    PROMPT_TEMPLATE="$DEFAULT_PROMPT"
    echo "Using default prompt (no $PROMPT_FILE found)"
fi

# Substitute ${PLAN_FILE} in prompt
PROMPT=$(echo "$PROMPT_TEMPLATE" | PLAN_FILE="$PLAN_FILE" envsubst '${PLAN_FILE}')

echo "==========================="
echo "Ralph Wiggum - Iterative Implementation"
echo "Plan: $PLAN_FILE"
echo "Max iterations: $MAX_ITERATIONS"
echo "==========================="
echo ""
export CLAUDE_CODE_TASK_LIST_ID=kenmore-experiments
export  CLAUDE_CODE_ENABLE_TASKS=1

while [ $ITERATION -le $MAX_ITERATIONS ]; do
    echo "==========================="
    echo "Starting iteration $ITERATION"
    echo "==========================="

    echo "$PROMPT" | claude -p \
        --dangerously-skip-permissions \
        --output-format=stream-json \
        --verbose \
        | npx repomirror visualize

    echo ""
    echo "==========================="
    echo "Completed iteration $ITERATION"
    echo "==========================="
    echo ""

    ITERATION=$((ITERATION + 1))

    if [ $ITERATION -le $MAX_ITERATIONS ]; then
        echo "Sleeping for $SLEEP_SECONDS seconds before next iteration..."
        sleep $SLEEP_SECONDS
    fi
done

if [ $ITERATION -gt $MAX_ITERATIONS ]; then
    echo "Reached maximum iterations ($MAX_ITERATIONS). Review progress manually."
fi

echo ""
echo "Plan file: $PLAN_FILE"
echo "Prompt file: $PROMPT_FILE"
