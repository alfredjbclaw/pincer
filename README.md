# openclaw-maintainer-skills

OpenClaw-flavored maintainer skills for keeping software projects healthy with a coding-agent in the loop. Two skills:

- **`gh-triage`** — per-repo (or cross-owner) GitHub queue triage. Reads open issues, PRs, CI, and author trust signals; classifies every item into one of three buckets and emits a maintainer-facing report. Does not act; it just decides what's actionable.
- **`repo-orchestrator`** — multi-repo control plane. Wakes on an interval, runs `gh-triage` per repo, delegates autonomous work to OpenClaw subagent workers, gates merges on live proof, and gates releases on an empty effective queue. Asks the owner only for decision-ready items (land/delete / pick alternative / supply credential).

## Install

```bash
clawhub install openclaw-maintainer-skills
```

Or clone manually and symlink each skill directory into your OpenClaw skills root (typically `~/.openclaw/workspace/skills/`).

## Use

From inside any GitHub repo checkout:

```text
triage
```

The `gh-triage` skill picks up the current repo, scans the open queue, and prints a URL-first report with three buckets: **Autonomous**, **Needs owner**, **Defer/close**.

For continuous oversight across multiple repos:

```text
orchestrate <owner1> <owner2> ...
```

`repo-orchestrator` builds a cross-repo ledger and runs the triage→delegate→monitor→release loop.

## Credit

These skills are an OpenClaw-flavored adaptation of [`steipete/agent-scripts`](https://github.com/steipete/agent-scripts) by Peter Steinberger ([@steipete](https://github.com/steipete)). The original skills target the Codex CLI; this fork targets OpenClaw's agent runtime (subagent spawns instead of Codex threads, vanilla `gh` instead of a local RepoBar binary, parameterized owner instead of `steipete`).

Improvements proposed back to upstream are tracked in `CHANGELOG.md`.

## License

MIT. See [`LICENSE`](./LICENSE).
