# Conference Networking Prep Agent (eventprep)

A Claude agent that prepares you for one day at a professional conference: who to meet, what to know about them, what to say, and where to be.

**Framework**: Anthropic SDK (no agent framework)  
**LLM**: Claude Opus 5.5  
**Search**: Claude's built-in web search and web fetch tools

## What it does

1. Reads the event programme, either a URL (fetched only if `robots.txt` allows) or a page you saved from your browser
2. Extracts every session and speaker for the day you attend
3. Ranks speakers against your 2–4 goals and drops anyone you exclude
4. Researches each top person on public pages and keeps only facts it can tie to a source URL from the search. Professional networking and contact-scraping sites are blocked.
5. Writes an outreach draft per person. Each draft is under 600 characters, contains no flattery, and uses one verified detail. If nothing was verified, there's no draft.
6. Builds a day plan with time clashes flagged, plus icebreaker questions and a glossary of the programme's terms

It never sends messages and never logs in anywhere. A fact whose source wasn't returned by the search, or that may be about someone else with the same name, is labelled **unverified** and never used in a draft.

## Setup

Requires Python 3.9+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your API key
```

Get an API key: https://console.anthropic.com → API Keys

## Run

```bash
# From a programme URL
python agent.py --url https://example-conf.com/programme --day "Wednesday" \
  --goal "meet startup founders" --goal "meet people building AI agents"

# From a page you saved (use this when a site's terms forbid automated access)
python agent.py --programme-file programme.html --event "Example Conf 2026" --day 2026-11-04 \
  --goal "meet startup founders" --goal "meet people building AI agents" \
  --about "Student exploring AI agents." --exclude "Main Stage" --top 5
```

| Flag | Meaning |
|---|---|
| `--goal` | Repeat 2–4 times, most important first |
| `--about` | Who you are, used for the tone of the drafts |
| `--exclude` | Name, company or stage to skip (repeatable) |
| `--top` | How many people to research (default 5) |
| `--out` | Output folder (default `output/`, git-ignored) |

The pack is written to `output/<event>-<day>/pack.md`, with the full research and every source in `research.json` next to it.

**Runtime**: about 3–6 minutes for `--top 5`.

## Sample Output

```
📋 Reading the programme for Wednesday...
   65 sessions found
🎯 Ranking people against your goals...
    88  Jane Example (Example Robotics)
    82  Sam Sample (Sample AI)
🔍 Researching 2 people on public pages...
✍️  Drafting messages from verified facts only...
🗓️  Planning the day...
============================================================
📄 EVENT PACK: output/example-conf-2026-wednesday/pack.md
============================================================
```

```markdown
### 1. Jane Example, CEO & Co-founder, Example Robotics
10:30 · Theater 2 · Safety layers for autonomous machines

- Example Robotics makes safety software for autonomous farm machines. ([source](https://example.com/about))
- *Unverified:* Jane studied at Example University.

**Draft (402 characters):**

> Hi Jane, I read that Example Robotics adds a safety layer so farm machines can run without
> someone watching. I'm a student exploring AI agents, and when a machine may act alone is the
> question I keep coming back to. Could we talk for 10 minutes after your 10:30 session? [name]
```

## Architecture

```
Programme (URL or saved file)
  → [Extract] Claude → sessions for your day (JSON)
  → [Rank]    Claude → top people vs. your goals, exclusions applied in code
  → [Research] Claude + web_search / web_fetch → facts with source URLs
               code keeps a fact only if its URL came back from the search
  → [Draft]   Claude → <600-char message from verified facts only
  → [Plan]    Claude → day plan, icebreakers, glossary
  → pack.md + research.json
```

## Privacy

`output/` and `.env` are git-ignored. The pack contains research on real people, so keep it to yourself. Only names, roles, companies and public professional facts relevant to a conversation are stored.
