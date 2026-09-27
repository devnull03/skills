# claude-skills

Claude Code skills I've written, one per directory under `skills/`.

| Skill | What it does |
|---|---|
| [run-colab](skills/run-colab/) | Drive Colab GPU VMs from the terminal: create, push code (verified), run detached jobs, watch and pull results incrementally, contact sheets, stop |
| [colab-tailscale-api](skills/colab-tailscale-api/) | Turn a Colab project into a private HTTP API on a Tailscale Service with a URL that survives VM restarts |

## Install

Claude Code loads personal skills from `~/.claude/skills/<name>/SKILL.md` and
follows symlinks, so link each skill from this repo:

```bash
git clone https://github.com/devnull03/claude-skills ~/claude-skills
for s in ~/claude-skills/skills/*/; do ln -sfn "$s" ~/.claude/skills/"$(basename "$s")"; done
```

Edits in the repo take effect immediately, with nothing to reinstall.
