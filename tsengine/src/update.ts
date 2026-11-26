// Self-update: check the latest GitHub release, fetch "What's new".
// TS port of the update-check half of platform_extras.py (download/install is OS-specific
// and handled by Tauri's updater in the packaged app).

import { BUILD } from "./config";

const REPO = process.env.LC4_REPO || "lightcode4/incredible-herness";

type Release = { tag: string; build: number; body: string; url: string };

async function latestRelease(): Promise<Release | null> {
  try {
    const res = await fetch(`https://api.github.com/repos/${REPO}/releases?per_page=15`, {
      headers: { accept: "application/vnd.github+json", "user-agent": "LightCode4" },
    });
    if (!res.ok) return null;
    const rels = (await res.json()) as any[];
    // newest numeric build-<n> tag
    let best: Release | null = null;
    for (const r of rels) {
      const m = /(?:^|-)build-(\d+)$/.exec(r.tag_name || "");
      const n = m ? Number(m[1]) : NaN;
      if (!Number.isNaN(n) && (!best || n > best.build)) {
        best = { tag: r.tag_name, build: n, body: r.body || "", url: r.html_url || "" };
      }
    }
    return best;
  } catch {
    return null;
  }
}

export async function checkUpdate(): Promise<{ update: boolean; latest?: number; url?: string }> {
  const rel = await latestRelease();
  if (rel && rel.build > BUILD) return { update: true, latest: rel.build, url: rel.url };
  return { update: false };
}

export async function whatsNew(): Promise<{ html: string; build: number | null; url: string }> {
  const rel = await latestRelease();
  if (!rel) return { html: "<p>Release notes will appear here once published.</p>", build: null, url: `https://github.com/${REPO}/releases` };
  // very small markdown → HTML (headings, bullets, links)
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const html = rel.body.split("\n").map((ln) => {
    const h = /^(#{1,3})\s+(.*)/.exec(ln);
    if (h) return `<h${h[1]!.length}>${esc(h[2]!)}</h${h[1]!.length}>`;
    const li = /^\s*[-*]\s+(.*)/.exec(ln);
    if (li) return `<li>${esc(li[1]!)}</li>`;
    return ln.trim() ? `<p>${esc(ln)}</p>` : "";
  }).join("\n").replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`);
  return { html, build: rel.build, url: rel.url };
}
