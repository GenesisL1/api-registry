## What this changes

<!-- One or two sentences. What is different afterwards? -->

## Why

<!-- The problem, not the patch. Link an issue if there is one. -->

## Checklist

- [ ] `./test/run-all.sh` passes locally
- [ ] Contract changed? `./scripts/build.sh` re-run and the generated files committed
- [ ] New behaviour has a test that would fail without the change
- [ ] Docs updated if this changes how the thing is deployed or operated

## If this touches the contract

- [ ] Deploy bytecode still under 24,576 bytes (CI checks, but confirm you saw it)
- [ ] No new external call on an operator's write path
- [ ] No new way for one party to change another's reliability record
- [ ] Storage layout change noted below, since the registry is not upgradeable

<!-- Anything a reviewer should look at hardest: -->
