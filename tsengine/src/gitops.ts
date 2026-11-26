// Git for the Source control panel: status, diff, commit, branches, checkout, push.
// TS port of gitops.py.

function git(cwd: string, args: string[], check = true): string {
  const p = Bun.spawnSync(["git", ...args], { cwd, stdin: "ignore" });
  const out = p.stdout.toString();
  if (check && p.exitCode !== 0) {
    throw new Error(p.stderr.toString().trim() || `git ${args.join(" ")} failed`);
  }
  return out;
}

function root(cwd: string): string | null {
  try { return git(cwd, ["rev-parse", "--show-toplevel"]).trim(); } catch { return null; }
}

export function status(cwd: string) {
  const top = root(cwd);
  if (!top) return { repo: false };
  const branch = git(cwd, ["branch", "--show-current"]).trim() || "(detached)";
  const files = [];
  for (const line of git(cwd, ["status", "--porcelain=v1", "-uall"]).split("\n")) {
    if (!line) continue;
    const code = line.slice(0, 2);
    let path = line.slice(3);
    if (path.includes(" -> ")) path = path.split(" -> ")[1]!;
    const kind = code.includes("?") || code.includes("A") ? "added"
      : code.includes("D") ? "deleted" : code.includes("R") ? "renamed" : "modified";
    files.push({ path: path.replace(/^"|"$/g, ""), status: kind, code });
  }
  let upstream = git(cwd, ["rev-parse", "--abbrev-ref", "@{u}"], false).trim();
  let ahead = 0, behind = 0;
  if (upstream && !upstream.includes("fatal")) {
    const counts = git(cwd, ["rev-list", "--left-right", "--count", "HEAD...@{u}"], false).split(/\s+/).filter(Boolean);
    if (counts.length === 2) { ahead = Number(counts[0]); behind = Number(counts[1]); }
  } else upstream = "";
  const branches = git(cwd, ["branch", "--format=%(refname:short)"], false).split("\n").map((b) => b.trim()).filter(Boolean);
  const last = git(cwd, ["log", "-1", "--format=%h %s"], false).trim();
  return { repo: true, root: top, branch, files, upstream, ahead, behind, branches, last_commit: last, gh: false };
}

export function diff(cwd: string, path?: string): string {
  const out = git(cwd, ["diff", "HEAD", "--", ...(path ? [path] : [])], false);
  return out.slice(0, 200_000);
}

export function commit(cwd: string, message: string, files?: string[]): string {
  if (!message.trim()) throw new Error("Write a commit message first.");
  if (files && files.length) git(cwd, ["add", "--", ...files]);
  else git(cwd, ["add", "-A"]);
  return (git(cwd, ["commit", "-m", message]).trim().split("\n")[0]) || "Committed";
}

export function checkout(cwd: string, branch: string, create = false): string {
  return git(cwd, ["checkout", ...(create ? ["-b"] : []), branch]).trim() || `On ${branch}`;
}

export function push(cwd: string): string {
  const branch = git(cwd, ["branch", "--show-current"]).trim();
  return git(cwd, ["push", "-u", "origin", branch], false).trim() || "Pushed.";
}

export function action(cwd: string, data: any) {
  switch (data?.action) {
    case "diff": return { diff: diff(cwd, data.path || undefined) };
    case "commit": return { commit: commit(cwd, String(data.message || ""), data.files) };
    case "checkout": return { ...status(cwd), message: checkout(cwd, String(data.branch || ""), Boolean(data.create)) };
    case "push": return { message: push(cwd), ...status(cwd) };
    default: return status(cwd);
  }
}
