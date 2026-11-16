# lumen-forge

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/plonz/lumen-forge/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/plonz/lumen-forge/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

Lumen Forge is a standalone, GitHub-compatible git forge in a single Python file (standard library only, no git binary). It serves four surfaces from its own in-memory git object store: git smart HTTP (upload-pack protocol v2 with a v0 fallback, receive-pack v0 with report-status and side-band-64k), the REST v3 API (repositories, branches, commits, contents, labels, issues, comments, pull requests and merges), the GraphQL v4 API (the gh CLI's query and mutation surface, Relay global ids, cursor pagination, schema introspection) and an MCP endpoint (JSON-RPC 2.0, six tools over the same store). Every timestamp comes from a deterministic logical clock and every id from a counter, so a trial and its replay produce byte-identical state.

## Run it

```sh
MAKEFUN_ENV_URL=http://127.0.0.1:8080 MAKEFUN_SHOP_TOKEN=<redacted> MAKEFUN_TRIAL_NOW=1735689600 python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=plonz&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>
