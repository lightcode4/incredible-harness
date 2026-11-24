"""Optional services: better web search, image generation, maps, cloud upload, office documents and media.

Everything that needs an account uses the key the user saved in Settings > Services. Nothing here sends data anywhere
unless the agent calls the matching tool (and the user allowed it).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

UA = {"User-Agent": "LightCode4/1.0"}


class ServiceError(Exception):
    pass


def _http(url: str, data: bytes | None = None, headers: dict | None = None, method: str | None = None, timeout: int = 90):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        body = e.read()[:600].decode(errors="replace")
        raise ServiceError(f"{e.code} from {urllib.parse.urlparse(url).netloc}: {body}")
    except Exception as e:
        raise ServiceError(f"Could not reach {urllib.parse.urlparse(url).netloc}: {e}")


def _json(url, payload=None, headers=None, method=None, timeout=90):
    raw, _ = _http(url, json.dumps(payload).encode() if payload is not None else None,
                   {"Content-Type": "application/json", **(headers or {})}, method, timeout)
    return json.loads(raw or b"{}")


# ---------------------------------------------------------------- web search providers
SEARCH_URLS = {"tavily": "https://api.tavily.com/search", "brave": "https://api.search.brave.com/res/v1/web/search",
               "exa": "https://api.exa.ai/search"}


def search(provider: str, query: str, key: str = "", searxng: str = "", base: str = "") -> list[dict] | None:
    """[{title, url, snippet}] from the chosen provider, or None to use the built-in search."""
    if provider == "tavily" and key:
        d = _json(base or SEARCH_URLS["tavily"], {"api_key": key, "query": query, "max_results": 8})
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:300]} for r in d.get("results", [])]
    if provider == "brave" and key:
        raw, _ = _http((base or SEARCH_URLS["brave"]) + "?" + urllib.parse.urlencode({"q": query, "count": 8}),
                       headers={"X-Subscription-Token": key, "Accept": "application/json"})
        d = json.loads(raw)
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": re.sub(r"<[^>]+>", "", r.get("description", ""))}
                for r in (d.get("web") or {}).get("results", [])]
    if provider == "exa" and key:
        d = _json(base or SEARCH_URLS["exa"], {"query": query, "numResults": 8, "contents": {"text": {"maxCharacters": 300}}},
                  {"x-api-key": key})
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": (r.get("text") or "")[:300]} for r in d.get("results", [])]
    if provider == "searxng" and searxng:
        raw, _ = _http(searxng.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"}))
        d = json.loads(raw)
        return [{"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")[:300]} for r in d.get("results", [])[:8]]
    return None


# ---------------------------------------------------------------- image generation
def generate_image(provider: str, key: str, prompt: str, size: str = "1024x1024", source: bytes | None = None,
                   base: str = "") -> bytes:
    """PNG/JPEG bytes. provider: openai | replicate | fal. `source` turns it into an edit of that image (OpenAI)."""
    if not key:
        raise ServiceError(f"Image generation needs a {provider or 'provider'} key: add it in Settings > Services.")
    if provider == "openai":
        root = base or "https://api.openai.com/v1"
        if source:
            boundary = "----lc4" + os.urandom(6).hex()
            def part(name, value, filename=None, ctype=None):
                head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
                head += f"\r\nContent-Type: {ctype}\r\n\r\n" if ctype else "\r\n\r\n"
                return head.encode() + (value if isinstance(value, bytes) else str(value).encode()) + b"\r\n"
            body = part("model", "gpt-image-1") + part("prompt", prompt) + part("size", size) + part("image", source, "image.png", "image/png")
            body += f"--{boundary}--\r\n".encode()
            raw, _ = _http(root + "/images/edits", body, {"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=180)
            d = json.loads(raw)
        else:
            d = _json(root + "/images/generations", {"model": "gpt-image-1", "prompt": prompt, "size": size, "n": 1},
                      {"Authorization": f"Bearer {key}"}, timeout=180)
        item = (d.get("data") or [{}])[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            return _http(item["url"])[0]
        raise ServiceError("The image service returned no image.")
    if provider == "replicate":
        root = base or "https://api.replicate.com/v1"
        d = _json(root + "/models/black-forest-labs/flux-schnell/predictions", {"input": {"prompt": prompt, "output_format": "png"}},
                  {"Authorization": f"Bearer {key}", "Prefer": "wait"}, timeout=180)
        for _ in range(60):
            if d.get("status") in ("succeeded", "failed", "canceled"):
                break
            time.sleep(2)
            d = _json(d["urls"]["get"], headers={"Authorization": f"Bearer {key}"})
        out = d.get("output")
        url = out[0] if isinstance(out, list) and out else out
        if d.get("status") != "succeeded" or not url:
            raise ServiceError(f"Replicate did not finish: {d.get('error') or d.get('status')}")
        return _http(url)[0]
    if provider == "fal":
        d = _json(base or "https://fal.run/fal-ai/flux/schnell", {"prompt": prompt}, {"Authorization": f"Key {key}"}, timeout=180)
        imgs = d.get("images") or []
        if not imgs:
            raise ServiceError("fal.ai returned no image.")
        return _http(imgs[0]["url"])[0]
    raise ServiceError("Choose an image provider in Settings > Services (OpenAI, Replicate or fal.ai).")


# ---------------------------------------------------------------- maps (OpenStreetMap: free, no key)
def geocode(query: str, base: str = "") -> list[dict]:
    raw, _ = _http((base or "https://nominatim.openstreetmap.org/search") + "?" +
                   urllib.parse.urlencode({"q": query, "format": "json", "limit": 6, "addressdetails": 0}))
    return [{"name": r.get("display_name", ""), "lat": float(r["lat"]), "lon": float(r["lon"]), "type": r.get("type", "")}
            for r in json.loads(raw)]


def places_nearby(lat: float, lon: float, kind: str, radius: int = 1000, base: str = "") -> list[dict]:
    """Points of interest from OpenStreetMap. kind is an amenity/shop/tourism value like cafe, restaurant, pharmacy, hotel."""
    kind = re.sub(r"[^a-z_]", "", kind.lower())[:30] or "cafe"
    radius = max(50, min(int(radius), 20000))
    q = (f'[out:json][timeout:25];(node(around:{radius},{lat},{lon})["amenity"="{kind}"];node(around:{radius},{lat},{lon})["shop"="{kind}"];'
         f'node(around:{radius},{lat},{lon})["tourism"="{kind}"];);out 30;')
    raw, _ = _http(base or "https://overpass-api.de/api/interpreter", urllib.parse.urlencode({"data": q}).encode(),
                   {"Content-Type": "application/x-www-form-urlencoded"})
    out = []
    for e in json.loads(raw).get("elements", []):
        t = e.get("tags", {})
        out.append({"name": t.get("name", "(unnamed)"), "lat": e.get("lat"), "lon": e.get("lon"),
                    "address": " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city")) if x),
                    "opening_hours": t.get("opening_hours", ""), "website": t.get("website", "")})
    return out


# ---------------------------------------------------------------- cloud upload (S3 / Cloudflare R2, AWS signature v4)
def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def s3_upload(cfg: dict, name: str, data: bytes, content_type: str = "application/octet-stream", expires: int = 604800) -> str:
    """Upload to an S3-compatible bucket. Returns the public URL (if a public base is set) or a 7-day signed link."""
    endpoint, bucket = (cfg.get("endpoint") or "").rstrip("/"), cfg.get("bucket", "")
    ak, sk, region = cfg.get("access_key", ""), cfg.get("secret_key", ""), cfg.get("region") or "auto"
    if not (endpoint and bucket and ak and sk):
        raise ServiceError("Cloud upload needs an endpoint, bucket and keys: add them in Settings > Services.")
    key = "lightcode4/" + time.strftime("%Y%m%d-%H%M%S-") + re.sub(r"[^\w.\-]+", "-", name)[-80:]
    host = urllib.parse.urlparse(endpoint).netloc
    path = f"/{bucket}/{urllib.parse.quote(key)}"
    now = dt.datetime.now(dt.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{day}/{region}/s3/aws4_request"
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz, "content-type": content_type}
    signed = ";".join(sorted(headers))
    canon = "\n".join(["PUT", path, "", "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload_hash])
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = _sign(_sign(_sign(_sign(("AWS4" + sk).encode(), day), region), "s3"), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
    _http(endpoint + path, data, {"Authorization": auth, "x-amz-content-sha256": payload_hash, "x-amz-date": amz,
                                  "Content-Type": content_type}, method="PUT", timeout=300)
    if cfg.get("public_base"):
        return cfg["public_base"].rstrip("/") + "/" + urllib.parse.quote(key)
    # presigned GET link
    q = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{ak}/{scope}", "X-Amz-Date": amz,
         "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host"}
    qs = "&".join(f"{urllib.parse.quote(a, safe='')}={urllib.parse.quote(b, safe='')}" for a, b in sorted(q.items()))
    canon = "\n".join(["GET", path, qs, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    return f"{endpoint}{path}?{qs}&X-Amz-Signature={sig}"


# ---------------------------------------------------------------- office documents (no extra libraries)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_office(data: bytes, name: str, max_chars: int = 200_000) -> str:
    """Text of a .docx (paragraphs and tables), .pptx (slide by slide) or .xlsx (sheet by sheet, as rows)."""
    ext = Path(name).suffix.lower()
    try:
        z = zipfile.ZipFile(__import__("io").BytesIO(data))
    except zipfile.BadZipFile:
        raise ServiceError(f"{name} is not a valid {ext} file.")
    out = []
    if ext == ".docx":
        root = ET.fromstring(z.read("word/document.xml"))
        for block in root.iter():
            if block.tag == W + "p" and not any(a.tag == W + "tc" for a in []):
                text = "".join(t.text or "" for t in block.iter(W + "t"))
                style = block.find(f"{W}pPr/{W}pStyle")
                if text.strip():
                    level = (style.get(W + "val") or "") if style is not None else ""
                    out.append(("#" * int(level[-1]) + " " if level.lower().startswith("heading") and level[-1:].isdigit() else "") + text)
    elif ext == ".pptx":
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
        for i, n in enumerate(slides, 1):
            root = ET.fromstring(z.read(n))
            paras = ["".join(t.text or "" for t in p.iter(A + "t")) for p in root.iter(A + "p")]
            out.append(f"## Slide {i}\n" + "\n".join(p for p in paras if p.strip()))
    elif ext == ".xlsx":
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(S + "t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        names = [s.get("name") for s in wb.iter(S + "sheet")]
        sheets = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
        for i, n in enumerate(sheets):
            root = ET.fromstring(z.read(n))
            rows = []
            for row in root.iter(S + "row"):
                cells = []
                for c in row.iter(S + "c"):
                    v = c.find(S + "v")
                    val = v.text if v is not None else "".join(t.text or "" for t in c.iter(S + "t"))
                    if c.get("t") == "s" and val is not None and val.isdigit() and int(val) < len(shared):
                        val = shared[int(val)]
                    cells.append(val or "")
                if any(cells):
                    rows.append(" | ".join(cells))
                if len(rows) >= 2000:
                    rows.append("… (more rows)")
                    break
            out.append(f"## Sheet: {names[i] if i < len(names) else i + 1}\n" + "\n".join(rows))
    else:
        raise ServiceError("Only .docx, .pptx and .xlsx are supported here (PDFs use read_pdf).")
    text = "\n\n".join(out).strip()
    return text[:max_chars] + ("\n… (truncated)" if len(text) > max_chars else "") if text else "(no text found)"


# ---------------------------------------------------------------- audio / video
def find_ffmpeg() -> str | None:
    for c in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if c and Path(c).is_file():
            return c
    return None


def transcribe(audio: bytes, filename: str, key: str, base: str = "") -> str:
    if not key:
        raise ServiceError("Transcribing audio needs an OpenAI key (Settings > Services or Model).")
    boundary = "----lc4" + os.urandom(6).hex()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper-1\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{Path(filename).name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    raw, _ = _http((base or "https://api.openai.com/v1") + "/audio/transcriptions", body,
                   {"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)
    return json.loads(raw).get("text", "")


def media_frames_and_audio(path: Path, frames: int = 6) -> tuple[list[bytes], bytes | None, float]:
    """Key frames (JPEG) spread through a video, its soundtrack (m4a) and its length in seconds."""
    ff = find_ffmpeg()
    if not ff:
        raise ServiceError("Reading video needs ffmpeg (Mac: brew install ffmpeg).")
    probe = subprocess.run([ff, "-i", str(path)], capture_output=True, text=True, timeout=60).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    tmp = Path(tempfile.mkdtemp(prefix="lc4media-"))
    shots = []
    if "Video:" in probe:
        for i in range(frames):
            t = dur * (i + 0.5) / frames if dur else i
            out = tmp / f"f{i}.jpg"
            subprocess.run([ff, "-y", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "4", str(out)],
                           capture_output=True, timeout=120)
            if out.exists():
                shots.append(out.read_bytes())
    audio = None
    if "Audio:" in probe:
        a = tmp / "audio.m4a"
        subprocess.run([ff, "-y", "-i", str(path), "-vn", "-ac", "1", "-b:a", "64k", str(a)], capture_output=True, timeout=600)
        audio = a.read_bytes() if a.exists() else None
    shutil.rmtree(tmp, ignore_errors=True)
    return shots, audio, dur
