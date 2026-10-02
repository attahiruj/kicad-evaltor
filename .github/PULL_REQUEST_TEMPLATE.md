## What this changes

<!-- One or two sentences. What is observably different after this merges? -->

## Why

<!-- The problem this solves, or a link to the issue. -->

Closes #

## Checklist

- [ ] `uv run ruff check .` is clean
- [ ] `uv run ruff format --check .` is clean
- [ ] `uv run mypy src` is clean
- [ ] `uv run pytest -m "not integration"` passes
- [ ] Added or updated tests cover the change
- [ ] Added a `CHANGELOG.md` entry under `Unreleased`, if this is user-visible
- [ ] Updated the README if a check id, name or behaviour changed

## Notes for the reviewer

<!-- Anything that is knowingly incomplete, risky, or that you could not verify
     locally. Saying "I could not verify this" is much more useful than a
     plausible-looking claim. -->

<!-- If this changes text geometry or a layout check, say how you verified it
     against a real KiCad rendering. -->
