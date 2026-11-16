# aitoolspub

MCP tools that give coding agents (Claude Code, Codex, or any MCP client) a real browser, real email and a view
into how AI search engines answer questions. Each tool runs on your own machine, with your own keys.

| Tool | What your agent gets |
|---|---|
| **Browser** | A shared, persistent Chrome (self-hosted [Steel](https://github.com/steel-dev/steel-browser) in Docker): open pages, read, fill forms, run JavaScript, hand you a live link to sign in. Optional captcha solving. |
| **Email** | Your Google Workspace mailboxes mirrored locally and kept in sync: instant search and regex grep, read, wait for a new message, send, reply, forward, label, archive. |
| **Search trace** | For SEO / AEO / GEO: the queries an AI search engine writes for a question, the pages it gets back, what it reads and what it cites; and whether a change to your page would change its answer. |

## Setup

Your agent does the setup. Paste one of these prompts into Claude Code, Codex or any agent that can run shell
commands. It clones this repo (or uses your copy), installs the tool, asks you for the keys it needs and adds the
tool to the agents you choose.

**Browser**

```text
Install the browser tool from https://github.com/Diffio-AI/aitoolspub by following setup/browser.md in that repo.
```

**Email**

```text
Install the email tool from https://github.com/Diffio-AI/aitoolspub by following setup/email.md in that repo.
```

**Search trace**

```text
Install the search-trace tool from https://github.com/Diffio-AI/aitoolspub by following setup/search-trace.md in that repo.
```

What each one needs:

| Tool | Needs | Guide |
|---|---|---|
| Browser | Docker. Optional: an OpenRouter key (the browsing navigator), NopeCHA and 2captcha keys (captcha solving) | [setup/browser.md](setup/browser.md) |
| Email | A Google Workspace domain you administer and a Google Cloud project (personal @gmail.com accounts aren't supported) | [setup/email.md](setup/email.md) |
| Search trace | OpenRouter and Exa keys. Optional: an OpenAI key | [setup/search-trace.md](setup/search-trace.md) |

Built and tested on Linux (systemd, Docker). On other systems your agent will have to adapt the service steps.

Keys stay on your machine, in `keys/` and `steel/secrets/` (both git-ignored), and are never written into agent
configs. To change how a tool works, ask your agent: each tool's own README (`browser/`, `mail/`, `search-trace/`,
`steel/`) explains how it's built.
