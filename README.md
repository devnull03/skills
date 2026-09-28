# skills

Claude Code skills I've written, one per directory under `skills/`.

| Skill | What it does |
|---|---|
| [run-colab](skills/run-colab/) | Drive Colab GPU VMs from the terminal: create, push code (verified), run detached jobs, watch and pull results incrementally, contact sheets, stop |
| [colab-tailscale-api](skills/colab-tailscale-api/) | Turn a Colab project into a private HTTP API on a Tailscale Service with a URL that survives VM restarts |

## Install

These follow the open Agent Skills layout (a directory with a `SKILL.md` that
has `name` and `description` frontmatter, plus its scripts), so they work in
Claude Code, Codex and any agent that reads that format.

```bash
git clone https://github.com/devnull03/skills ~/skills
~/skills/install.sh              # links into ~/.claude/skills and ~/.codex/skills
~/skills/install.sh ~/.agents/skills   # or any other agent's skills dir
```

The install links each skill rather than copying it, so edits in the clone take
effect everywhere immediately. An existing folder with the same name is skipped, not
overwritten. Scripts find each other relative to their own location, so any install
directory works.
