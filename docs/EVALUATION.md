# Evaluation

## Goal

Measure whether Katta produces grounded, useful research responses with minimal hallucination.

## Core Metrics

- Grounding rate: percent of claims traceable to tool outputs.
- URL integrity: percent of returned URLs that were tool-derived.
- Freshness: percent of responses using recent sources for time-sensitive queries.
- Actionability: human score (1-5) for usefulness.

## Suggested Benchmark Set

Create a fixed prompt set of 20-30 questions across:

- Current events
- Developer tooling
- Pricing/comparison
- Deep-niche community questions

## Procedure

1. Run each prompt against current branch.
2. Save model/tool transcript artifacts.
3. Score with a rubric and compare to previous baseline.
4. Track deltas per commit.

## Regression Gate

Block release if grounding rate or URL integrity regresses by >5%.
