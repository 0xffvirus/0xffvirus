#!/usr/bin/env python3
"""Render the fastfetch-style profile card shown in the README.

Writes fastfetch/dark.svg, fastfetch/light.svg and the link buttons.
Static fields live in PROFILE below; GitHub stats are fetched live.

    GH_TOKEN=$(gh auth token) python3 fastfetch/fastfetch.py   # refresh stats
    python3 fastfetch/fastfetch.py                               # no token: reuse last stats

Standard library only, so the GitHub Action needs no setup step.
"""

import calendar
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER = os.environ.get("GH_USER", "0xffvirus")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
CACHE = HERE / "cache.json"

PROMPT = "fastfetch"
TITLE = "bahaa@0xffvirus"
UPTIME_SINCE = dt.date(2005, 6, 3)  # birthday, so Uptime reads as your age
LOC_SKIP = set()  # repos to leave out of Lines of Code (none: every project counts)

# (key, value) is a dotted row, None is a blank row, ("-", "Name") starts a section.
# {uptime} and the GitHub Stats rows are filled in at render time.
PROFILE = [
    ("OS", "Omarchy"),
    ("Uptime", "{uptime}"),
    ("Host", "KFUPM"),
    ("Kernel", "Software Engineering Student"),
    ("IDE", "VSCode, Neovim"),
    ("Locale", "Dammam, Saudi Arabia"),
    None,
    ("Languages.Programming", "TypeScript, Python, Go, Dart"),
    ("Languages.Computer", "HTML, CSS, SQL, LaTeX, Markdown"),
    ("Languages.Real", "Arabic, English"),
    None,
    ("Focus.AI", "AI Agents, MCP, LLM Tooling"),
    ("Focus.Backend", "Go, Node.js, FastAPI, PostgreSQL"),
    ("Focus.Games", "Godot, Unity, Aseprite"),
    ("Focus.Mobile", "Flutter, iOS, Android"),
    ("Focus.Cloud", "AWS, Supabase, Vercel, Docker"),
    None,
    ("-", "Contact"),
    ("Linktree", "0xbahaa"),
    ("LinkedIn", "0xbahaa"),
    None,
    ("-", "GitHub Stats"),
    "STATS",
]

BUTTONS = [  # (file name, label) -> fastfetch/btn-<name>-<theme>.svg
    ("linktree", "LINKTREE"),
    ("linkedin", "LINKEDIN"),
]

# Layout, in characters and px. CW is the advance of a 0.6em monospace glyph.
WIDTH = 58  # info column
GAP = 3  # between art and info
FONT = 16
CW = FONT * 0.6
LINE = 20
PAD = 24

THEMES = {
    "dark": dict(bg="#161b22", border="#30363d", text="#c9d1d9", art="#7ee3ec", glint="#ff4d6d", key="#ffa657", val="#a5d6ff",
                 dim="#616e7f", add="#3fb950", sub="#f85149", prompt="#3fb950"),
    "light": dict(bg="#f6f8fa", border="#d0d7de", text="#24292f", art="#0e6e7a", glint="#d1243f", key="#953800", val="#0a3069",
                  dim="#8c959f", add="#1a7f37", sub="#cf222e", prompt="#1a7f37"),
}

FONT_STACK = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', 'DejaVu Sans Mono', monospace"


# --------------------------------------------------------------------------- GitHub


def request(url, payload=None):
    headers = {"Authorization": f"bearer {TOKEN}", "User-Agent": "fastfetch-readme",
               "Accept": "application/vnd.github+json"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return r.status, json.loads(raw) if raw.strip() else None


def graphql(query, **variables):
    _, body = request("https://api.github.com/graphql", {"query": query, "variables": variables})
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"]


REPOS_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    repositoriesContributedTo(first: 1, includeUserRepositories: false) { totalCount }
    repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner owner { login } isFork stargazerCount pushedAt }
    }
  }
}"""


def fetch_repos():
    repos, cursor = [], None
    while True:
        user = graphql(REPOS_QUERY, login=USER, cursor=cursor)["user"]
        page = user["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return user, repos
        cursor = page["pageInfo"]["endCursor"]


def fetch_commits(created):
    now = dt.datetime.now(dt.timezone.utc)
    fields = []
    for year in range(created.year, now.year + 1):
        end = min(dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc), now)
        fields.append(f'y{year}: contributionsCollection(from: "{year}-01-01T00:00:00Z", '
                      f'to: "{end:%Y-%m-%dT%H:%M:%SZ}") {{ totalCommitContributions }}')
    data = graphql(f'query($login: String!) {{ user(login: $login) {{ {" ".join(fields)} }} }}', login=USER)
    return sum(year["totalCommitContributions"] for year in data["user"].values())


def contributor_lines(name):
    """(additions, deletions) by USER in one repo, or None while GitHub is still computing."""
    try:
        status, body = request(f"https://api.github.com/repos/{name}/stats/contributors")
    except urllib.error.HTTPError as e:
        print(f"  skip {name}: HTTP {e.code}", file=sys.stderr)
        return 0, 0
    if status == 202:
        return None
    for contributor in body or []:
        if (contributor.get("author") or {}).get("login", "").lower() == USER.lower():
            weeks = contributor["weeks"]
            return sum(w["a"] for w in weeks), sum(w["d"] for w in weeks)
    return 0, 0


def fetch_lines(repos, cache):
    """Sum your additions/deletions across every repo, reusing cached counts for repos with no new pushes.

    Cache keys are hashed so private repo names never land in this public repo.
    """
    old, new = cache.get("loc", {}), {}
    pending = []
    for repo in repos:
        if repo["nameWithOwner"] in LOC_SKIP:
            continue
        key = hashlib.sha256(repo["nameWithOwner"].encode()).hexdigest()[:16]
        hit = old.get(key)
        if hit and hit["pushed"] == repo["pushedAt"]:
            new[key] = hit
        else:
            pending.append((key, repo))

    for attempt in range(8):
        waiting = []
        for key, repo in pending:
            counts = contributor_lines(repo["nameWithOwner"])
            if counts is None:
                waiting.append((key, repo))
            else:
                new[key] = {"pushed": repo["pushedAt"], "add": counts[0], "del": counts[1]}
        pending = waiting
        if not pending:
            break
        time.sleep(2 + attempt * 2)

    for key, repo in pending:  # GitHub never finished computing: fall back to the last known counts
        print("  stats still pending for a repo, using cached counts", file=sys.stderr)
        if key in old:
            new[key] = old[key]

    cache["loc"] = dict(sorted(new.items()))
    return sum(v["add"] for v in new.values()), sum(v["del"] for v in new.values())


def fetch_stats(cache):
    user, repos = fetch_repos()
    owned = [r for r in repos if r["owner"]["login"].lower() == USER.lower()]
    added, deleted = fetch_lines(repos, cache)
    return {
        "repos": len(owned),
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "stars": sum(r["stargazerCount"] for r in owned),
        "followers": user["followers"]["totalCount"],
        "commits": fetch_commits(dt.datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))),
        "added": added,
        "deleted": deleted,
    }


# --------------------------------------------------------------------------- text layout


def uptime(since, today):
    months = (today.year - since.year) * 12 + today.month - since.month
    if today.day < since.day:
        months -= 1
    y, m = divmod(since.month - 1 + months, 12)
    anchor = dt.date(since.year + y, m + 1, min(since.day, calendar.monthrange(since.year + y, m + 1)[1]))
    days = (today - anchor).days
    parts = [(months // 12, "year"), (months % 12, "month"), (days, "day")]
    return ", ".join(f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in parts)


def seg_len(segments):
    return sum(len(text) for text, _ in segments)


def dotted(key, value, width, lead=". "):
    """`. Key: ........ value` padded to exactly `width` characters."""
    value = [(value, "val")] if isinstance(value, str) else value
    dots = width - len(lead) - len(key) - 3 - seg_len(value)
    if dots < 1:
        raise SystemExit(f"'{key}' is {1 - dots} chars too long for a {width}-wide column")
    return [(lead, "dim"), (key, "key"), (":", "dim"), (" " + "." * dots + " ", "dim"), *value]


def rule(title, width, lead=""):
    return [(lead, "dim"), (title, "title"), (" " + "-" * (width - len(lead) - len(title) - 1), "dim")]


def stats_rows(s, width):
    left = 34
    right = width - left - 3
    sep = [(" | ", "dim")]
    repos = [(f"{s['repos']:,}", "val"), (" {", "dim"), ("Contributed", "key"), (": ", "dim"),
             (f"{s['contributed']:,}", "val"), ("}", "dim")]
    loc = [(f"{s['added'] - s['deleted']:,}", "val"), (" ( ", "dim"),
           (f"{s['added']:,}++", "add"), (", ", "dim"), (f"{s['deleted']:,}--", "sub"), (" )", "dim")]
    return [
        dotted("Repos", repos, left) + sep + dotted("Stars", f"{s['stars']:,}", right, lead=""),
        dotted("Commits", f"{s['commits']:,}", left) + sep + dotted("Followers", f"{s['followers']:,}", right, lead=""),
        dotted("Lines of Code", loc, width),
    ]


def info_rows(stats, today):
    rows = [rule(TITLE, WIDTH)]
    for item in PROFILE:
        if item is None:
            rows.append([])
        elif item == "STATS":
            rows += stats_rows(stats, WIDTH)
        elif item[0] == "-":
            rows.append(rule(item[1], WIDTH, lead="- "))
        else:
            rows.append(dotted(item[0], item[1].format(uptime=uptime(UPTIME_SINCE, today)), WIDTH))
    return rows


# --------------------------------------------------------------------------- SVG


def style(t, extra=""):
    return f"""<style>
text {{ font-family: {FONT_STACK}; font-size: {FONT}px; white-space: pre; fill: {t['text']}; }}
.key {{ fill: {t['key']}; }} .val {{ fill: {t['val']}; }} .dim {{ fill: {t['dim']}; }}
.title {{ fill: {t['key']}; font-weight: 700; }} .add {{ fill: {t['add']}; }} .sub {{ fill: {t['sub']}; }}
.prompt {{ fill: {t['prompt']}; font-weight: 700; }}{extra}
.art {{ fill: {t.get('art', t['text'])}; }} .glint {{ fill: {t.get('glint', t['sub'])}; font-weight: 700; }}
</style>"""


def text_row(x, y, segments, attrs=""):
    attr = f" {attrs}" if attrs else ""
    spans = "".join(f'<tspan class="{c}">{escape(s)}</tspan>' if c else escape(s) for s, c in segments if s)
    return f'<text x="{x:g}" y="{y:g}"{attr}>{spans}</text>'


def load_art():
    """ascii.txt rows as segments; characters wrapped in {braces} are drawn in the accent colour."""
    rows = []
    for line in (HERE / "ascii.txt").read_text(encoding="utf-8").rstrip("\n").split("\n"):
        segments = []
        for i, chunk in enumerate(line.replace("}", "{").split("{")):
            segments.append((chunk, "glint" if i % 2 else None))
        rows.append(segments)
    return rows


def card(theme, art, rows):
    t = THEMES[theme]
    art_cols = max(map(seg_len, art))
    total_rows = len(rows) + 2  # prompt above and below
    width = round(PAD * 2 + (art_cols + GAP + WIDTH) * CW)
    height = PAD * 2 + total_rows * LINE
    baseline = lambda row: PAD + row * LINE + FONT * 0.8
    info_x = PAD + (art_cols + GAP) * CW
    art_top = 1 + (len(rows) - len(art)) / 2

    blink = """
.cursor { animation: blink 1.1s steps(1) infinite; }
@keyframes blink { 50% { fill-opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .cursor { animation: none; } }"""
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xml:space="preserve" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title">',
        f'<title id="title">{escape(TITLE)} — fastfetch</title>',
        style(t, blink),
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" '
        f'fill="{t["bg"]}" stroke="{t["border"]}"/>',
        text_row(PAD, baseline(0), [("❯ ", "prompt"), (PROMPT, None)]),
    ]
    out += [text_row(PAD, baseline(art_top + i), line, 'class="art"') for i, line in enumerate(art)]
    out += [text_row(info_x, baseline(1 + i), segs) for i, segs in enumerate(rows) if segs]
    out.append(text_row(PAD, baseline(total_rows - 1), [("❯ ", "prompt"), ("█", "cursor")]))
    out.append("</svg>\n")
    return "\n".join(out)


def button(theme, label):
    t = THEMES[theme]
    w, h = 960 // len(BUTTONS), 48  # keeps the label size steady however many buttons share the row
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(label)}">
{style(t)}
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="8" fill="{t['bg']}" stroke="{t['border']}"/>
<rect x="12" y="{h - 3}" width="{w - 24}" height="2" rx="1" fill="{t['key']}"/>
{text_row(w / 2, h / 2 + FONT * 0.35, [("[ ", "dim"), (label, "key"), (" ]", "dim")], 'text-anchor="middle"')}
</svg>
"""


def main():
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    if TOKEN:
        cache["stats"] = fetch_stats(cache)
    elif "stats" not in cache:
        raise SystemExit("No GH_TOKEN and no cached stats: run once with GH_TOKEN=$(gh auth token)")
    else:
        print("No GH_TOKEN, reusing the last fetched stats", file=sys.stderr)
    CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    art = load_art()
    rows = info_rows(cache["stats"], dt.datetime.now(dt.timezone.utc).date())
    for theme in THEMES:
        (HERE / f"{theme}.svg").write_text(card(theme, art, rows), encoding="utf-8")
        for name, label in BUTTONS:
            (HERE / f"btn-{name}-{theme}.svg").write_text(button(theme, label), encoding="utf-8")
    print(json.dumps(cache["stats"]))


if __name__ == "__main__":
    main()
