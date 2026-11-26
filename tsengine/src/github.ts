// GitHub connection: device-flow sign-in, personal-token connect, profile + repos.
// TS port of github.py (the parts the Settings > GitHub UI uses).

export const API = process.env.LC4_GITHUB_API || "https://api.github.com";
export const WEB = process.env.LC4_GITHUB_WEB || "https://github.com";
export const CLIENT_ID = process.env.LC4_GITHUB_CLIENT_ID || "Ov23litVtPUQm2QSkJNZ";
export const SCOPES = "repo workflow read:org gist";
export const TOKEN_URL =
  "https://github.com/settings/tokens/new?description=LightCode4&scopes=repo,workflow,read:org,gist";

export class GitHubError extends Error {}

export type GitHubUser = { login: string; name: string; avatar: string; url: string; id: number | null };

async function api(token: string, method: string, path: string, body?: unknown): Promise<any> {
  if (!token) throw new GitHubError("GitHub isn't connected. Connect it in Settings > GitHub.");
  const res = await fetch(API + (path.startsWith("/") ? path : "/" + path), {
    method,
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/vnd.github+json",
      "user-agent": "LightCode4",
      "x-github-api-version": "2022-11-28",
      ...(body ? { "content-type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = "";
    try { detail = ((await res.json()) as any).message || ""; } catch {}
    if (res.status === 401) throw new GitHubError("GitHub rejected the token (it may have expired). Reconnect in Settings > GitHub.");
    throw new GitHubError(`GitHub said ${res.status}: ${detail || res.statusText}`);
  }
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

export async function whoami(token: string): Promise<GitHubUser> {
  const u = await api(token, "GET", "/user");
  return { login: u.login || "", name: u.name || "", avatar: u.avatar_url || "", url: u.html_url || "", id: u.id ?? null };
}

export type Repo = { full_name: string; private: boolean; description: string; updated: string; url: string; branch: string };

export async function repos(token: string): Promise<Repo[]> {
  const out: Repo[] = [];
  for (const page of [1, 2, 3]) {
    const batch: any[] = await api(token, "GET",
      `/user/repos?sort=updated&per_page=100&page=${page}&affiliation=owner,collaborator,organization_member`);
    for (const r of batch) out.push({
      full_name: r.full_name, private: !!r.private, description: r.description || "",
      updated: r.pushed_at || r.updated_at || "", url: r.html_url || "", branch: r.default_branch || "main",
    });
    if (batch.length < 100) break;
  }
  return out;
}

// ---- device flow ----
async function form(url: string, fields: Record<string, string>): Promise<any> {
  const res = await fetch(url, {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/x-www-form-urlencoded", "user-agent": "LightCode4" },
    body: new URLSearchParams(fields).toString(),
  });
  if (!res.ok) throw new GitHubError(`GitHub sign-in failed (${res.status}). Check the OAuth app has Device Flow enabled.`);
  return res.json();
}

export async function deviceStart(clientId: string): Promise<{ device_code: string; user_code: string; verification_uri: string; interval: number; expires_in: number }> {
  if (!clientId) throw new GitHubError("Sign in with GitHub isn't set up in this build. Use a token instead.");
  const r = await form(WEB + "/login/device/code", { client_id: clientId, scope: SCOPES });
  if (!r.device_code) throw new GitHubError(r.error_description || "GitHub didn't start the sign-in.");
  return r;
}

export async function devicePoll(
  clientId: string, deviceCode: string, interval: number, expiresIn: number, stopped: () => boolean,
): Promise<string> {
  const deadline = Date.now() + expiresIn * 1000;
  let wait = interval;
  while (Date.now() < deadline) {
    if (stopped()) throw new GitHubError("Sign-in cancelled.");
    await new Promise((r) => setTimeout(r, wait * 1000));
    const r = await form(WEB + "/login/oauth/access_token", {
      client_id: clientId, device_code: deviceCode, grant_type: "urn:ietf:params:oauth:grant-type:device_code",
    });
    if (r.access_token) return r.access_token;
    if (r.error === "slow_down") wait = Number(r.interval) || wait + 5;
    else if (r.error === "access_denied") throw new GitHubError("You declined the sign-in on GitHub.");
    else if (r.error === "expired_token") break;
    else if (r.error && r.error !== "authorization_pending") throw new GitHubError(r.error_description || `GitHub sign-in failed (${r.error}).`);
  }
  throw new GitHubError("The sign-in code expired. Start again.");
}

// ---- shareable pages (GitHub Pages) ----
const PAGES_REPO = "lightcode4-pages";

export async function publishPage(token: string, login: string, title: string, html: string): Promise<string> {
  if (!login) throw new GitHubError("Connect GitHub first (Settings > GitHub).");
  try { await api(token, "GET", `/repos/${login}/${PAGES_REPO}`); }
  catch { await api(token, "POST", "/user/repos", { name: PAGES_REPO, auto_init: true, private: false, description: "Pages shared from LightCode4" }); }
  const slug = (title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60)) || "page";
  const path = `${slug}.html`;
  let sha: string | undefined;
  try { sha = (await api(token, "GET", `/repos/${login}/${PAGES_REPO}/contents/${path}`)).sha; } catch {}
  const body: any = { message: `Publish ${title}`, content: Buffer.from(html).toString("base64") };
  if (sha) body.sha = sha;
  await api(token, "PUT", `/repos/${login}/${PAGES_REPO}/contents/${path}`, body);
  try { await api(token, "POST", `/repos/${login}/${PAGES_REPO}/pages`, { source: { branch: "main", path: "/" } }); } catch {}
  return `https://${login.toLowerCase()}.github.io/${PAGES_REPO}/${path}`;
}

export async function unpublishPage(token: string, login: string, url: string): Promise<void> {
  const path = url.split("/").pop()!;
  const sha = (await api(token, "GET", `/repos/${login}/${PAGES_REPO}/contents/${path}`)).sha;
  await api(token, "DELETE", `/repos/${login}/${PAGES_REPO}/contents/${path}`, { message: `Unpublish ${path}`, sha });
}

// The private no-reply commit email GitHub uses, so commits link to the profile.
export function identity(user: GitHubUser | null): [string, string] {
  const login = user?.login || "";
  if (!login) return ["", ""];
  const email = user?.id ? `${user.id}+${login}@users.noreply.github.com` : `${login}@users.noreply.github.com`;
  return [user?.name || login, email];
}
