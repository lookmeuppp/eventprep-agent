"""
eventprep: Conference Networking Prep Agent (Claude API + web search).

Reads an event programme, ranks the people worth meeting against your goals,
researches each one on public pages, and writes an event pack: short briefs
with a source URL on every fact, outreach drafts under 600 characters,
a day plan, icebreakers and a glossary.

It never sends messages, never logs in anywhere, and skips professional
networking sites. A fact without a source returned by the search is marked
"unverified" and is never used in a draft.

Usage:
    python agent.py --url https://example.com/programme --day "Wednesday" \
        --goal "meet startup founders" --goal "meet people working on AI agents"
    python agent.py --programme-file saved_programme.html --day 2026-11-04 \
        --goal "meet startup founders" --about "Student exploring AI agents." --top 5
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"
USER_AGENT = "eventprep-agent/1.0 (personal conference prep; respects robots.txt)"
BLOCKED_DOMAINS = ["linkedin.com", "xing.com", "rocketreach.co", "contactout.com", "zoominfo.com"]
MAX_DRAFT_CHARS = 600
BETAS = ["server-side-fallback-2026-07-01"]


def load_env(path: str = ".env") -> None:
    """Minimal .env loader so the agent needs no extra dependency."""
    if not os.path.exists(path):
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------- programme input ----------

def html_to_text(raw: str) -> str:
    raw = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", raw, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def fetch_programme(url: str) -> str:
    parts = urllib.parse.urlparse(url)
    robots = urllib.robotparser.RobotFileParser(f"{parts.scheme}://{parts.netloc}/robots.txt")
    robots.read()
    if not robots.can_fetch(USER_AGENT, url):
        sys.exit(f"robots.txt disallows fetching {url}. Save the page in your browser and use --programme-file.")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


# ---------- Claude calls ----------

def ask(client: anthropic.Anthropic, prompt: str, schema: dict | None = None, tools: list | None = None,
        system: str = "", effort: str = "medium") -> anthropic.types.beta.BetaMessage:
    """One streamed request. Resumes pause_turn for server tools and stops on refusal."""
    messages: list = [{"role": "user", "content": prompt}]
    output_config: dict = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    kwargs: dict = dict(model=MODEL, max_tokens=64000, system=system or anthropic.NOT_GIVEN,
                        messages=messages, output_config=output_config, betas=BETAS, fallbacks="default")
    if tools:
        kwargs["tools"] = tools
    for _ in range(5):
        with client.beta.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Request declined: {response.stop_details}")
        if response.stop_reason != "pause_turn":
            return response
        messages = [messages[0], {"role": "assistant", "content": response.content}]
        kwargs["messages"] = messages
    return response


def text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text")


def ask_json(client, prompt: str, schema: dict, **kw) -> dict:
    return json.loads(text_of(ask(client, prompt, schema=schema, **kw)))


def obj(props: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


STR = {"type": "string"}
PERSON = obj({"name": STR, "role": STR, "company": STR})
SESSION = obj({"time": STR, "stage": STR, "title": STR, "speakers": {"type": "array", "items": PERSON}})


def extract_sessions(client, programme: str, day: str) -> list[dict]:
    schema = obj({"sessions": {"type": "array", "items": SESSION}})
    prompt = (f"Below is a conference programme. List every session on this day: {day}. "
              "Copy times, stages, titles, speaker names, roles and companies exactly as written; "
              "use an empty string when a field is missing.\n\n<programme>\n" + programme + "\n</programme>")
    return ask_json(client, prompt, schema)["sessions"]


def is_excluded(target: dict, excludes: list[str]) -> bool:
    haystack = " ".join([target.get("name", ""), target.get("company", ""), target.get("stage", "")]).lower()
    return any(e.lower() in haystack for e in excludes if e.strip())


def rank_targets(client, sessions: list[dict], goals: list[str], excludes: list[str], top: int) -> list[dict]:
    target = obj({"name": STR, "role": STR, "company": STR, "time": STR, "stage": STR,
                  "session_title": STR, "why": STR, "score": {"type": "integer"}})
    schema = obj({"targets": {"type": "array", "items": target}})
    prompt = ("Rank the speakers below by how useful meeting them is for these goals, "
              "most important goal first:\n" + "\n".join(f"{i}. {g}" for i, g in enumerate(goals, 1)) +
              f"\nSkip anyone matching these exclusions: {excludes or 'none'}. Score 0-100. "
              f"Return the best {top * 2}, with a one-sentence reason each.\n\n" + json.dumps(sessions, ensure_ascii=False))
    ranked = ask_json(client, prompt, schema)["targets"]
    ranked = [t for t in ranked if not is_excluded(t, excludes)]
    return sorted(ranked, key=lambda t: -t["score"])[:top]


def research(client, person: dict) -> dict:
    """Web-search one person, then keep only facts whose source the search actually returned."""
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5,
              "blocked_domains": BLOCKED_DOMAINS},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3,
              "blocked_domains": BLOCKED_DOMAINS}]
    notes = ask(client, (
        f"Research {person['name']} ({person['role']}, {person['company']}) for a short, friendly "
        "conversation at a conference. Open their company's own website first, then personal or university "
        "pages and news. Find what the company does, one or two recent concrete things (launches, funding, "
        "talks, papers) and their professional background. Only count a page as being about this person "
        f"if it names both {person['name']} and {person['company']}, or is {person['company']}'s own site. "
        "Ignore contact details and anything private. For every fact, write the exact URL it came from."),
        tools=tools)
    seen_urls = set()
    for block in notes.content:
        if block.type == "web_search_tool_result" and isinstance(block.content, list):
            seen_urls.update(r.url for r in block.content)
        if block.type == "web_fetch_tool_result" and getattr(block.content, "url", None):
            seen_urls.add(block.content.url)
        for citation in getattr(block, "citations", None) or []:
            if getattr(citation, "url", None):
                seen_urls.add(citation.url)
    fact = obj({"text": STR, "source_url": STR, "about_this_person": {"type": "boolean"}})
    schema = obj({"facts": {"type": "array", "items": fact}, "conflicts": {"type": "array", "items": STR}})
    data = ask_json(client, (
        f"Turn these research notes on {person['name']} of {person['company']} into short, plain facts, "
        "each with the one URL that supports it. Set about_this_person to true only when the source names "
        f"both {person['name']} and {person['company']}, or is {person['company']}'s own site; otherwise false. "
        "Write no hedges inside the fact text. Leave out flattery and anything you cannot tie to a URL. "
        "Put disagreements between sources under conflicts.\n\n" + text_of(notes)), schema, effort="low")
    for f in data["facts"]:
        f["verified"] = f.pop("about_this_person") and f["source_url"] in seen_urls
    return {**person, **data}


def draft_message(client, person: dict, about: str) -> str:
    facts = [f for f in person["facts"] if f["verified"]]
    if not facts:
        return ""
    schema = obj({"message": STR})
    prompt = (f"Write a short outreach message from me to {person['name']} asking for a 10-minute chat at the event "
              f"(their session: {person['session_title']} at {person['time']}). About me: {about}\n"
              "Rules: under 550 characters; mention exactly one specific detail from the facts below; "
              "no flattery and no claims about me beyond 'About me'; end with '[name]'.\n\n"
              + json.dumps(facts, ensure_ascii=False))
    for _ in range(2):
        message = ask_json(client, prompt, schema, effort="low")["message"].strip()
        if len(message) < MAX_DRAFT_CHARS:
            return message
        prompt += f"\n\nYour last draft was {len(message)} characters. Make it shorter."
    return ""


def plan_day(client, sessions: list[dict], people: list[dict], goals: list[str]) -> dict:
    slot = obj({"time": STR, "stage": STR, "what": STR, "why": STR})
    term = obj({"term": STR, "meaning": STR})
    schema = obj({"plan": {"type": "array", "items": slot},
                  "icebreakers": {"type": "array", "items": STR},
                  "glossary": {"type": "array", "items": term}})
    prompt = ("Make a day plan for this conference day that puts me in the sessions of the target people, "
              "flags time clashes, and leaves time to network. Then write 8 short, open icebreaker questions "
              "for people I know nothing about, and a glossary of 10-16 terms from the programme that a "
              "student should know. Goals: " + "; ".join(goals) +
              "\n\nTargets:\n" + json.dumps([{k: p[k] for k in ("name", "time", "stage", "session_title")} for p in people],
                                             ensure_ascii=False) +
              "\n\nSessions:\n" + json.dumps(sessions, ensure_ascii=False))
    return ask_json(client, prompt, schema)


# ---------- output ----------

def render(event: str, day: str, people: list[dict], extras: dict) -> str:
    out = [f"# {event} · {day}", "", "## Day plan", ""]
    out += [f"- **{s['time']}** · {s['stage']} · {s['what']} - {s['why']}" for s in extras["plan"]]
    out += ["", "## People", ""]
    for i, p in enumerate(people, 1):
        out += [f"### {i}. {p['name']}, {p['role']}, {p['company']}",
                f"{p['time']} · {p['stage']} · {p['session_title']}", "", f"*Why:* {p['why']}", ""]
        verified = [f for f in p["facts"] if f["verified"]]
        out += [f"- {f['text']} ([source]({f['source_url']}))" for f in verified]
        out += [f"- *Unverified:* {f['text']}" for f in p["facts"] if not f["verified"]]
        if not verified:
            out.append("- Nothing verified found. Look up manually, or use the icebreakers.")
        out += [f"- *Sources disagree:* {c}" for c in p["conflicts"]]
        if p.get("draft"):
            out += ["", f"**Draft ({len(p['draft'])} characters):**", "", "> " + p["draft"]]
        out.append("")
    out += ["## Icebreakers", ""] + [f"{i}. {q}" for i, q in enumerate(extras["icebreakers"], 1)]
    out += ["", "## Glossary", ""] + [f"- **{t['term']}**: {t['meaning']}" for t in extras["glossary"]]
    return "\n".join(out) + "\n"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "event"


def main():
    parser = argparse.ArgumentParser(description="eventprep: conference networking prep agent")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Programme page URL (fetched only if robots.txt allows)")
    source.add_argument("--programme-file", help="Programme page you saved from your browser (.html or .txt)")
    parser.add_argument("--event", default="", help="Event name for the pack title")
    parser.add_argument("--day", required=True, help='Day you attend, e.g. "Wednesday" or 2026-11-04')
    parser.add_argument("--goal", action="append", required=True, help="Repeat 2-4 times, most important first")
    parser.add_argument("--about", default="A student interested in AI.", help="Who you are, for draft tone")
    parser.add_argument("--exclude", action="append", default=[], help="Name, company or stage to skip")
    parser.add_argument("--top", type=int, default=5, help="How many people to research")
    parser.add_argument("--out", default="output", help="Output folder (git-ignored)")
    args = parser.parse_args()

    load_env()
    client = anthropic.Anthropic()

    raw = Path(args.programme_file).read_text(encoding="utf-8", errors="replace") if args.programme_file \
        else fetch_programme(args.url)
    programme = html_to_text(raw) if "<" in raw[:2000] else raw
    event = args.event or (urllib.parse.urlparse(args.url).netloc if args.url else Path(args.programme_file).stem)

    print(f"\n📋 Reading the programme for {args.day}...")
    sessions = extract_sessions(client, programme, args.day)
    print(f"   {len(sessions)} sessions found")

    print("🎯 Ranking people against your goals...")
    people = rank_targets(client, sessions, args.goal, args.exclude, args.top)
    for p in people:
        print(f"   {p['score']:3d}  {p['name']} ({p['company']})")

    print(f"🔍 Researching {len(people)} people on public pages...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        people = list(pool.map(lambda p: research(client, p), people))

    print("✍️  Drafting messages from verified facts only...")
    for p in people:
        p["draft"] = draft_message(client, p, args.about)

    print("🗓️  Planning the day...")
    extras = plan_day(client, sessions, people, args.goal)

    folder = Path(args.out) / slugify(f"{event}-{args.day}")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pack.md").write_text(render(event, args.day, people, extras), encoding="utf-8")
    (folder / "research.json").write_text(json.dumps(people, ensure_ascii=False, indent=1), encoding="utf-8")

    print("=" * 60)
    print(f"📄 EVENT PACK: {folder / 'pack.md'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
