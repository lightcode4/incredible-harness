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
