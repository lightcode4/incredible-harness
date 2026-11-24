"""Ready-made skills the user can add with one click (Gallery). Each becomes ~/.incredible-herness/skills/<id>/SKILL.md."""
from __future__ import annotations

import shutil
from pathlib import Path

SKILLS_DIR = Path.home() / ".incredible-herness" / "skills"

SKILLS = [
    {"id": "code-review", "name": "Code review", "blurb": "Review changes for bugs, security issues and unclear code, ranked by severity.",
     "body": """Review the code the user points at (default: the uncommitted changes, `git diff HEAD`).

1. Read the changed files in full, not just the diff, so you understand the context.
2. Look for, in this order: bugs and wrong behaviour; security problems (injection, secrets, unsafe input handling);
   data loss or race conditions; missing error handling; unclear names or logic; missing tests.
3. For every finding give: file:line, what is wrong, a concrete failure scenario, and the fix.
4. Rank findings most severe first. Say plainly when you found nothing serious. Do not pad the list with style nits.
5. Do not change files unless the user asks you to apply the fixes."""},
    {"id": "write-tests", "name": "Write tests", "blurb": "Add focused tests using the project's own test setup, then run them.",
     "body": """Write tests for the code the user names.

1. Find how the project already tests (test folders, pytest/jest/vitest/go test config, package.json scripts). Match it.
2. Cover the main behaviour, edge cases (empty, large, invalid input) and one or two failure paths.
3. Keep each test small and named after the behaviour it checks.
4. Run the tests. If one fails, decide whether the test or the code is wrong; fix the test, or report the bug.
5. Finish with what is covered and how to run the tests."""},
    {"id": "debug", "name": "Debugging", "blurb": "Reproduce, find the root cause, fix it and prove the fix.",
     "body": """Fix the problem the user describes, methodically.

1. Reproduce it first (run the failing command, test or page). Quote the exact error.
2. Form a hypothesis, then check it by reading code, adding a temporary log, or running a smaller case.
3. Fix the root cause, not the symptom. Keep the change small.
4. Re-run the reproduction to prove it is fixed, and run the related tests.
5. Remove temporary logging. Explain the cause in one or two sentences."""},
    {"id": "commit-messages", "name": "Commit messages", "blurb": "Clear, conventional commit messages from the actual diff.",
     "body": """When asked to commit or to write a commit message:

1. Read `git status` and `git diff --staged` (or `git diff HEAD` if nothing is staged).
2. Write a subject line in the imperative mood, at most 72 characters, describing the user-visible change
   (e.g. "Fix crash when the cart is empty"). Use a conventional prefix (feat:, fix:, docs:, refactor:, test:, chore:)
   only if the repository already uses them.
3. Add a short body when the why is not obvious: what changed and why, not how.
4. Only commit when the user asked you to commit; never push unless asked."""},
    {"id": "explain-codebase", "name": "Explain a codebase", "blurb": "A guided tour: structure, main flows, where to start.",
     "body": """Explain the project in the current folder to someone new to it.

1. Look at the README, the manifest (package.json, pyproject.toml, go.mod, Cargo.toml...), the folder layout and the entry points.
2. Describe: what it does, how it is organised (main folders and what lives where), the main flow from entry point
   to output, how to run it and how to test it.
3. Point to the 5-10 most important files with one line each.
4. Keep it short and concrete; use a ```mermaid diagram if the flow has several moving parts."""},
    {"id": "security-review", "name": "Security review", "blurb": "Check for common vulnerabilities and leaked secrets.",
     "body": """Do a focused security review of the project or the files the user names.

Check for: secrets committed to the repo (keys, tokens, passwords, .env files); injection (SQL, shell, path traversal,
template); missing authentication or authorization checks; unsafe deserialization; XSS and unsafe HTML; weak crypto or
hand-rolled crypto; dependencies with known problems (if a lockfile and audit tool are available, run it).

For each finding: file:line, the risk, how it could be exploited, and the fix. Rank by severity. Never print full
secret values; show only the first few characters."""},
    {"id": "docs-writer", "name": "Documentation", "blurb": "Write or update READMEs and docs that match the code.",
     "body": """Write or update documentation for the code the user names.

1. Read the code first; document what it actually does, not what you assume.
2. A good README has: one-line description, install, quick start with a runnable example, configuration, and how to
   run tests. Keep examples copy-pasteable.
3. Match the project's existing tone and format. Prefer short sections and lists over long paragraphs.
4. Check every command and file path you mention exists."""},
    {"id": "release-notes", "name": "Release notes", "blurb": "Turn git history into readable notes for users.",
     "body": """Write release notes from the git history.

1. Find the range: since the last tag (`git describe --tags --abbrev=0`) unless the user gives one.
2. Read `git log --oneline` for that range, and the diffs of anything unclear.
3. Group into: New, Improved, Fixed, and (only if needed) Breaking changes with what users must do.
4. Write for users, not developers: say what changed for them. Skip internal refactors and test-only commits."""},
]
SKILLS += [
    {"id": "data-analysis", "name": "Data analysis", "blurb": "Explore a dataset, find what matters and chart it.",
     "body": """Analyse the data the user gives (CSV, Excel, JSON or a database).

1. Load it and report its shape: rows, columns, types, missing values, obvious errors. Use Python (pandas if available).
2. Answer the user's question first; if there is none, find the 3-5 most useful facts (trends, outliers, comparisons).
3. Back every claim with a number. Say when the data is too small or messy to be sure.
4. Show results as a chart with an ```echarts block (or a small table). One chart per point; label the axes.
5. Save any cleaned data or scripts in the project so the work can be repeated."""},
    {"id": "deep-research", "name": "Research", "blurb": "Investigate a topic across sources and write a cited brief.",
     "body": """Research the user's question thoroughly.

1. Break it into 3-6 sub-questions. Search each (web_search), then read the best sources (fetch_url); prefer primary sources.
2. Note what each source says and when it was published; flag disagreements.
3. Write a brief: a short answer first, then the evidence by sub-question, then open questions.
4. Cite every factual claim with a link. Say plainly what you could not verify."""},
    {"id": "frontend-design", "name": "Frontend design", "blurb": "Build polished, accessible web pages and components.",
     "body": """Build front-end work that looks designed, not generated.

1. Pick a clear visual direction (type scale, 1-2 accent colours, spacing scale) and use it consistently.
2. Semantic HTML, responsive layout (works at 375px and 1440px), visible focus states, sufficient contrast, alt text.
3. Prefer the project's framework and components; otherwise plain HTML/CSS with no build step.
4. Preview it (open_preview or an ```html artifact) and check it in the browser; fix layout issues before finishing."""},
    {"id": "brand-voice", "name": "Brand guidelines", "blurb": "Write and design in a consistent brand voice and look.",
     "body": """Apply the user's brand consistently.

1. Look for brand material first: project reference files, a brand guide, CSS variables, logos. Ask once if none exists.
2. Summarise the brand in 5 lines: voice (3 adjectives), words to use and avoid, colours (hex), typefaces, logo rules.
3. Apply it to every piece you write or design; point out anything off-brand in existing material.
4. Keep a BRAND.md in the project with the summary so future chats stay consistent."""},
    {"id": "file-organizer", "name": "File organizer", "blurb": "Tidy a messy folder safely, with a plan you approve first.",
     "body": """Organise the folder the user names, safely.

1. List what is there (sizes, types, dates). Find duplicates by content (hash), not by name.
2. Propose a structure and a move plan as a table (from -> to). Do NOT move or delete anything until the user approves.
3. After approval, move files (never delete; put suspected junk in a _to_review folder). Keep a log file of every move.
4. Report what changed and how to undo it."""},
    {"id": "internal-comms", "name": "Internal comms", "blurb": "Clear updates, announcements and summaries for a team.",
     "body": """Write internal communication that people actually read.

1. Lead with the point: what happened / what is needed, and by when.
2. Then context in 2-4 short bullets, then owners and next steps.
3. Match the format: status update, announcement, incident summary, meeting notes or weekly digest.
4. Plain words, no jargon, under 200 words unless asked. Offer a one-line version for chat."""},
]
BY_ID = {s["id"]: s for s in SKILLS}


def catalog() -> list[dict]:
    return [{"id": s["id"], "name": s["name"], "blurb": s["blurb"], "installed": (SKILLS_DIR / s["id"] / "SKILL.md").is_file()}
            for s in SKILLS]


def install(sid: str) -> None:
    s = BY_ID.get(sid)
    if not s:
        raise ValueError("Unknown skill.")
    d = SKILLS_DIR / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {sid}\ndescription: {s['blurb']}\n---\n# {s['name']}\n\n{s['body']}\n", encoding="utf-8")


def remove(sid: str) -> None:
    if sid not in BY_ID:
        raise ValueError("Unknown skill.")
    shutil.rmtree(SKILLS_DIR / sid, ignore_errors=True)
