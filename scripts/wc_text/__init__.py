"""The app's pure text logic, ported from src/utils (backend_authority.md
phase 2).

Each module mirrors one TypeScript file, function for function, and must
agree with it byte for byte: `scripts/parity/fixtures/*.json` hold inputs and
outputs produced by the TypeScript (src/parity/__tests__/parity.test.ts), and
test_parity.py runs the same inputs through these modules. The TypeScript
suites are the specification; a difference is a bug here, not a choice.

Known difference: lengths and slices count code points here and UTF-16 units
in JavaScript, so a truncation boundary can move by one next to an astral
character (an emoji). `context_ledger.hash_content` is exact.
"""
