<!-- SPDX-License-Identifier: MIT -->
## What and why

<!-- One or two lines. Link the issue if there is one. -->

## Checklist

- [ ] I have signed the CLA (my GitHub username is in `CLA-SIGNERS`) — see [CONTRIBUTING.md](../CONTRIBUTING.md)
- [ ] New source files carry the `SPDX-License-Identifier` of their directory
- [ ] Unit tests pass: `.venv/bin/python tests/test_lifecycle.py` (and the others in CI)
- [ ] No secrets in the diff: tokens, API keys, ntfy topics, hostnames

## Touches credentials?

If this changes credential injection, the vault, or link tokens
(`image/deskd.py` login/inject, `control-plane/store.py`, `control-plane/links.py`),
say which invariant in [SECURITY.md](../SECURITY.md) you checked and how.
