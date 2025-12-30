# Team Announcement - MCP Gateway

Hey team! 👋

I wanted to share something I've been working on that could be really useful for our Claude Code workflows:

**MCP Gateway** - A meta-server that eliminates tool bloat in Claude Code by acting as a single gateway to all your MCP servers.

## What's the problem it solves?

When Claude Code connects to multiple MCP servers (GitHub, Jira, databases, etc.), it loads ALL tool schemas into context, causing:
- Tool bloat (dozens of tools consuming context tokens)
- Static configuration (requires restarts to see new servers)
- No progressive disclosure

## What does MCP Gateway do?

- Exposes only 9 stable meta-tools instead of 50+ individual tools
- Auto-starts essential servers (Playwright, Context7) with zero config
- Dynamically provisions 25+ servers on-demand from a manifest
- Uses progressive disclosure (compact cards first, detailed schemas only when needed)
- No more restarting Claude Code to add new servers!

## Where to get it:

**PyPI**: https://pypi.org/project/pmcp/
```bash
pip install pmcp
# or
uvx pmcp
```

**GitHub**: https://github.com/ViperJuice/pmcp

## I'd love your feedback:

1. **Try it out** - Install it and see if it improves your Claude Code experience
2. **Submit issues/feature requests** on GitHub if you find bugs or have ideas
3. **Give me your honest opinion** - Does this solve a real problem for you?
4. **Social media?** - If you think it's valuable enough, should we introduce it publicly as a **Frontier Strategies software contribution** to the dev community?

Looking forward to hearing your thoughts!
