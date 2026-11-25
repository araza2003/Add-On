#!/usr/bin/env python3
"""
Add-On - push big Odoo modules to GitHub from a friendly local web UI.

Run:      python3 odoo-pusher.py
Install:  python3 odoo-pusher.py --install     (adds an app-menu launcher)

Needs only: python3, git, rsync  (sudo apt install git rsync)
Optional:   zenity (native folder picker; preinstalled on GNOME Ubuntu)
"""
import base64, fnmatch, html, json, os, re, secrets, shutil, subprocess, sys, tempfile
import threading, urllib.error, urllib.request, uuid, webbrowser, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

APP_DIR = Path.home() / ".config" / "odoo-pusher"
PROFILES = APP_DIR / "profiles.json"
UPLOAD_ROOT = Path(tempfile.gettempdir()) / "odoo-pusher-uploads"
PORT = 8765
APP_TOKEN = secrets.token_urlsafe(24)   # protects the local API from other websites
EXCLUDES = [".git", "__pycache__", "*.pyc", "node_modules", ".idea", ".vscode", ".DS_Store"]
BIG_FILE = 95 * 1024 * 1024             # GitHub rejects files over 100 MB
JOBS = {}


# ============================== BRANDING ==============================
# Edit these values to rebrand the whole app (name, links, contact, colour).
BRAND = {
    "name": "Add-On",
    "tagline": "Ship big Odoo modules to GitHub, without the upload limit",
    "author": "Ahmed Raza",
    "linkedin": "https://www.linkedin.com/in/ahmed-raza-193686146",
    "github": "https://github.com/araza2003",
    "email": "ar.razaahmed22@gmail.com",
    "phone": "+92 31522999226",
    "accent": "#2c5499",   # main brand colour (hex), navy blue to match the logo
}
# The logo images are embedded at the bottom of this file (LOGO ASSETS).
# ======================================================================


def render_page():
    esc_ = html.escape
    b = BRAND
    png = "data:image/png;base64,"
    rep = {
        "__APP_TOKEN__": APP_TOKEN,
        "__BRAND_NAME_URL__": quote(b["name"]),
        "__BRAND_NAME__": esc_(b["name"]),
        "__TAGLINE__": esc_(b["tagline"]),
        "__AUTHOR__": esc_(b["author"]),
        "__LINKEDIN__": esc_(b["linkedin"], quote=True),
        "__GITHUB__": esc_(b["github"], quote=True),
        "__EMAIL__": esc_(b["email"], quote=True),
        "__PHONE_TEL__": re.sub(r"[^\d+]", "", b["phone"]),
        "__PHONE__": esc_(b["phone"]),
        "__ACCENT__": b["accent"],
        "__LOGO_MARK__": png + LOGO_MARK_B64,
        "__WORDMARK__": png + WORDMARK_B64,
        "__FAVICON__": png + FAVICON_B64,
    }
    out = PAGE
    for k, v in rep.items():
        out = out.replace(k, v)
    return out


class ApiError(Exception):
    pass


# ----------------------------------------------------------------- profiles
def read_profiles():
    try:
        return json.loads(PROFILES.read_text())
    except Exception:
        return {}


def write_profiles(d):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(APP_DIR, 0o700)
    tmp = PROFILES.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, PROFILES)


def public(p):
    return {k: p.get(k, "") for k in ("id", "username", "name", "avatar", "label")}


def get_profile(pid):
    p = read_profiles().get(pid)
    if not p:
        raise ApiError("Unknown account. Add it again.")
    return p


# ------------------------------------------------------------------ GitHub
def gh(token, path):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "User-Agent": "odoo-pusher", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r), r.headers
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("message", "")
        except Exception:
            msg = ""
        if e.code == 401:
            msg = "GitHub rejected this token (invalid or expired)."
        raise ApiError(f"GitHub error {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise ApiError(f"Cannot reach GitHub: {e.reason}")


def list_repos(token):
    out = []
    for page in range(1, 11):
        data, _ = gh(token, f"/user/repos?per_page=100&page={page}&sort=pushed"
                            "&affiliation=owner,collaborator,organization_member")
        out += [{"name": r["full_name"], "private": r["private"], "branch": r["default_branch"]} for r in data]
        if len(data) < 100:
            break
    return out


def list_branches(token, repo):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ApiError("Repository must look like owner/name.")
    out = []
    for page in range(1, 11):
        data, _ = gh(token, f"/repos/{repo}/branches?per_page=100&page={page}")
        out += [b["name"] for b in data]
        if len(data) < 100:
            break
    return out


# ---------------------------------------------------------------- scanning
def excluded(name):
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDES)


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024


def scan(path):
    root = Path(path).expanduser()
    if not root.is_dir():
        raise ApiError("Folder not found.")
    mods, total, files, big = [], 0, 0, []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not excluded(d)]
        rel = Path(dp).relative_to(root)
        if "__manifest__.py" in fn:
            mods.append(root.name if str(rel) == "." else str(rel))
        for f in fn:
            if excluded(f):
                continue
            try:
                s = (Path(dp) / f).lstat().st_size
            except OSError:
                continue
            total += s
            files += 1
            if s > BIG_FILE:
                big.append(f"{rel / f} ({human(s)})")
    return {"path": str(root), "modules": sorted(mods), "files": files,
            "size": human(total), "big": big}


# --------------------------------------------------------------- push job
class Job:
    def __init__(self):
        self.log, self.status, self.url, self.secrets = [], "running", "", []

    def add(self, line):
        for s in self.secrets:
            line = line.replace(s, "***")
        self.log.append(line.rstrip())


def run(job, cmd, cwd=None, label=None):
    job.add("$ " + (label or " ".join(cmd)))
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    for line in proc.stdout:
        if line.strip():
            job.add(line)
    if proc.wait() != 0:
        raise ApiError(f"Command failed: {label or cmd[0]}")


def do_push(job, p, o):
    tmp = Path(tempfile.mkdtemp(prefix="odoo-pusher-"))
    try:
        token = p["token"]
        auth = base64.b64encode(f"{p['username']}:{token}".encode()).decode()
        job.secrets = [token, auth]
        gitc = ["git", "-c", f"http.extraHeader=Authorization: Basic {auth}",
                "-c", "http.postBuffer=524288000"]
        repo, branch = o["repo"], o["branch"]
        url = f"https://github.com/{repo}.git"
        src = Path(o["path"]).expanduser()
        if not src.is_dir():
            raise ApiError("Source folder not found.")
        if not shutil.which("rsync"):
            raise ApiError("rsync is missing. Run: sudo apt install rsync")
        subdir = o.get("subdir", "").strip().strip("/")
        if ".." in Path(subdir).parts:
            raise ApiError("Subfolder cannot contain '..'.")

        job.add(f"Pushing {src} -> {repo} ({branch})")
        r = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True)
        if r.returncode != 0 or branch.startswith("-"):
            raise ApiError("Invalid branch name.")

        # 1. Does the branch exist? (also verifies the token works)
        ls = subprocess.run(gitc + ["ls-remote", "--heads", url, branch],
                            capture_output=True, text=True)
        if ls.returncode != 0:
            err = ls.stderr.replace(token, "***").replace(auth, "***")
            raise ApiError("Cannot access the repository. Check the token's permissions.\n" + err)
        exists = bool(ls.stdout.strip())

        # 2. Clone (keeps the remote README, so there is never a merge conflict)
        work = tmp / "repo"
        if exists:
            run(job, gitc + ["clone", "--depth", "1", "--branch", branch, url, str(work)],
                label=f"git clone --depth 1 --branch {branch} {url}")
        else:
            run(job, gitc + ["clone", "--depth", "1", url, str(work)], label=f"git clone {url}")
            run(job, ["git", "checkout", "-b", branch], cwd=work)
            job.add(f"(branch '{branch}' did not exist, it will be created)")
        run(job, ["git", "config", "user.name", p.get("name") or p["username"]], cwd=work)
        run(job, ["git", "config", "user.email",
                  f"{p.get('uid', 0)}+{p['username']}@users.noreply.github.com"], cwd=work)

        # 3. Copy files
        dest = work / subdir if subdir else work
        dest.mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", "-a"]
        if o.get("mirror"):
            cmd.append("--delete")
        cmd.append("--exclude=.git")
        if o.get("clean", True):
            cmd += [f"--exclude={e}" for e in EXCLUDES if e != ".git"]
        cmd += [str(src) + "/", str(dest) + "/"]
        run(job, cmd, label="copy files into the repo")

        # 4. Commit
        run(job, ["git", "add", "-A"], cwd=work)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=work).returncode == 0:
            job.add("Nothing changed: the repository already matches your folder.")
            job.status = "done"
            job.url = f"https://github.com/{repo}/tree/{branch}"
            return
        run(job, ["git", "diff", "--cached", "--shortstat"], cwd=work)
        run(job, ["git", "commit", "-q", "-m", o["message"] or "Update modules"], cwd=work)

        # 5. Push
        run(job, gitc + ["push", "-u", "origin", branch], cwd=work,
            label=f"git push -u origin {branch}")
        job.add("Done.")
        job.url = f"https://github.com/{repo}/tree/{branch}"
        job.status = "done"
    except ApiError as e:
        job.add("ERROR: " + str(e))
        job.status = "error"
    except Exception as e:
        job.add("ERROR: " + repr(e))
        job.status = "error"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            if str(Path(o["path"]).resolve()).startswith(str(UPLOAD_ROOT.resolve())):
                rel = Path(o["path"]).resolve().relative_to(UPLOAD_ROOT.resolve())
                shutil.rmtree(UPLOAD_ROOT / rel.parts[0], ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------- HTTP API
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def _auth(self):
        return self._host_ok() and self.headers.get("X-App-Token") == APP_TOKEN

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _route(self, fn):
        if not self._auth():
            return self._send(403, {"error": "Forbidden"})
        try:
            self._send(200, fn())
        except ApiError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": repr(e)})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/":
            if not self._host_ok():
                return self._send(403, {"error": "Forbidden"})
            page = render_page().encode()
            return self._send(200, raw=page, ctype="text/html; charset=utf-8")
        if u.path == "/api/profiles":
            return self._route(lambda: [public(p) for p in read_profiles().values()])
        if u.path == "/api/repos":
            return self._route(lambda: list_repos(get_profile(q.get("profile", ""))["token"]))
        if u.path == "/api/branches":
            return self._route(lambda: list_branches(get_profile(q.get("profile", ""))["token"],
                                                     q.get("repo", "")))
        if u.path == "/api/job":
            def job_state():
                j = JOBS.get(q.get("id", ""))
                if not j:
                    raise ApiError("Unknown job.")
                return {"status": j.status, "url": j.url, "log": "\n".join(j.log)}
            return self._route(job_state)
        self._send(404, {"error": "Not found"})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/profiles/"):
            def rm():
                d = read_profiles()
                d.pop(u.path.rsplit("/", 1)[1], None)
                write_profiles(d)
                return {"ok": True}
            return self._route(rm)
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if u.path == "/api/profiles":
            def add():
                b = self._json()
                token = (b.get("token") or "").strip()
                if not token:
                    raise ApiError("Paste a token first.")
                user, hdr = gh(token, "/user")
                d = read_profiles()
                pid = next((k for k, v in d.items() if v["token"] == token), uuid.uuid4().hex[:8])
                d[pid] = {"id": pid, "username": user["login"], "name": user.get("name") or user["login"],
                          "avatar": user["avatar_url"], "uid": user["id"], "token": token,
                          "label": (b.get("label") or "").strip()}
                write_profiles(d)
                return public(d[pid])
            return self._route(add)

        if u.path == "/api/browse":
            def browse():
                if not shutil.which("zenity"):
                    raise ApiError("zenity not installed. Type the path instead.")
                r = subprocess.run(["zenity", "--file-selection", "--directory",
                                    "--title=Select the folder with your modules"],
                                   capture_output=True, text=True)
                return {"path": r.stdout.strip()}
            return self._route(browse)

        if u.path == "/api/scan":
            return self._route(lambda: scan(self._json().get("path", "")))

        if u.path == "/api/upload":
            def upload():
                name = re.sub(r"[^\w.-]", "_", q.get("name", "upload.zip"))
                folder = UPLOAD_ROOT / uuid.uuid4().hex[:8]
                folder.mkdir(parents=True)
                zp = folder / name
                left = int(self.headers.get("Content-Length") or 0)
                with open(zp, "wb") as f:
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                if not zipfile.is_zipfile(zp):
                    shutil.rmtree(folder, ignore_errors=True)
                    raise ApiError("That is not a valid .zip file.")
                out = folder / "files"
                with zipfile.ZipFile(zp) as z:
                    z.extractall(out)
                zp.unlink()
                entries = [e for e in out.iterdir() if e.name != "__MACOSX"]
                if len(entries) == 1 and entries[0].is_dir() and not (entries[0] / "__manifest__.py").exists():
                    out = entries[0]
                return {"path": str(out)}
            return self._route(upload)

        if u.path == "/api/push":
            def push():
                b = self._json()
                p = get_profile(b.get("profile", ""))
                if not re.fullmatch(r"[\w.-]+/[\w.-]+", b.get("repo", "")):
                    raise ApiError("Pick a repository (owner/name).")
                if not b.get("branch", "").strip():
                    raise ApiError("Enter a branch.")
                if not b.get("path", "").strip():
                    raise ApiError("Choose the folder to push.")
                job = Job()
                jid = uuid.uuid4().hex[:8]
                JOBS[jid] = job
                threading.Thread(target=do_push, args=(job, p, b), daemon=True).start()
                return {"id": jid}
            return self._route(push)

        self._send(404, {"error": "Not found"})


# -------------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__BRAND_NAME__ by __AUTHOR__</title>
<meta name="description" content="__TAGLINE__">
<link rel="icon" type="image/png" href="__FAVICON__">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<script>
(function(){var t=null;try{t=localStorage.getItem("theme")}catch(e){}
if(!t)t=matchMedia("(prefers-color-scheme: light)").matches?"light":"dark";
document.documentElement.dataset.theme=t})();
</script>
<style>
:root{
  --accent:__ACCENT__;
  --accent-2:color-mix(in srgb,var(--accent) 55%,#fff);
  --accent-soft:color-mix(in srgb,var(--accent) 16%,transparent);
  --ok:#2fbf84;--err:#e5484d;--warn:#d9930d;
  --ease:cubic-bezier(.22,1,.36,1);
  --display:"Bricolage Grotesque","IBM Plex Sans",system-ui,Ubuntu,sans-serif;
  --body:"IBM Plex Sans",system-ui,"Segoe UI",Ubuntu,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
html[data-theme=light]{color-scheme:light;--bg:#f3f6fb;--surface:#fff;--panel:#e9eff8;--text:#0f1a2e;--muted:#5a6a84;--line:#d2dcea;--field:#fff;--term:#0d1524;--termt:#d3e6d0;--on-accent:#fff}
html[data-theme=dark]{color-scheme:dark;--bg:#0a111d;--surface:#0f1828;--panel:#111c2f;--text:#e8eef8;--muted:#93a3bd;--line:#223049;--field:#0a1220;--term:#060b14;--termt:#cfe3c8;--on-accent:#fff}
*{box-sizing:border-box;margin:0}
body{font:15px/1.6 var(--body);background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:var(--accent-2)}
:focus-visible{outline:2px solid var(--accent-2);outline-offset:2px}

/* ---------- top bar ---------- */
.nav{display:flex;justify-content:space-between;align-items:center;padding:14px clamp(16px,4vw,40px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px;text-decoration:none;color:var(--text)}
.logo{width:40px;height:40px;flex:none;border-radius:11px;background:#f3f6fb;display:grid;place-items:center;border:1px solid color-mix(in srgb,var(--accent) 35%,transparent);transition:transform .5s var(--ease)}
.logo img{width:28px;height:28px;display:block}
.brand:hover .logo{transform:rotate(-8deg) scale(1.06)}
.brand b{display:block;font:700 17px/1.1 var(--display);letter-spacing:-.01em}
.brand small{color:var(--muted);font-size:12.5px}
.nav-r{display:flex;gap:8px;align-items:center}
.icon-btn{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;border:1px solid var(--line);background:transparent;color:var(--muted);cursor:pointer;transition:color .2s,border-color .2s,background .2s;padding:0}
.icon-btn:hover{color:var(--text);border-color:var(--accent);background:var(--accent-soft)}
.icon-btn svg{width:18px;height:18px;fill:currentColor}
#theme svg{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
html[data-theme=dark] .sun{display:none}html[data-theme=light] .moon{display:none}

/* ---------- hero + route (the one signature motion) ---------- */
.hero{max-width:1180px;margin:0 auto;padding:clamp(28px,5vw,56px) clamp(16px,4vw,40px) 8px}
.hero h1{font:700 clamp(30px,4.6vw,54px)/1.04 var(--display);letter-spacing:-.025em;max-width:16em}
.hero p{margin-top:14px;color:var(--muted);max-width:34em;font-size:16px}
.route{position:relative;margin-top:38px;padding-bottom:6px}
.stops{list-style:none;display:grid;grid-template-columns:repeat(5,1fr);padding:0;position:relative;z-index:1}
.stops li{display:flex;flex-direction:column;align-items:center;gap:9px;font-size:13px;color:var(--muted);transition:color .3s}
.stops i{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:var(--bg);border:2px solid var(--line);transition:background .4s var(--ease),border-color .4s var(--ease),transform .4s var(--ease);font-style:normal;font-size:0}
.stops li.done i{background:var(--accent);border-color:var(--accent)}
.stops li.done i::before{content:"\2713";font-size:14px;font-weight:700;color:var(--on-accent)}
.stops li.active{color:var(--text);font-weight:600}
.stops li.active i{border-color:var(--accent-2);transform:scale(1.12);animation:ping 1.6s ease-out infinite}
.stops li.error{color:var(--err);font-weight:600}
.stops li.error i{background:var(--err);border-color:var(--err)}
.stops li.error i::before{content:"!";font-size:15px;font-weight:700;color:#fff}
.track{position:absolute;top:14px;left:10%;right:10%;height:2px;background:var(--line);transform-origin:left;z-index:0}
.fill{position:absolute;inset:0 auto 0 0;width:calc(var(--p,0)*100%);background:var(--accent);transition:width .9s var(--ease)}
.packet{position:absolute;top:-5px;left:0;width:12px;height:12px;margin-left:-6px;border-radius:4px;background:var(--accent-2);box-shadow:0 0 0 4px var(--accent-soft),0 0 18px var(--accent);opacity:0}
.route.idle .packet{animation:travel 5s cubic-bezier(.65,0,.3,1) infinite 1.4s}
.route.running .packet,.route.done .packet{left:calc(var(--p,0)*100%);opacity:1;transition:left .9s var(--ease)}
.route.done .packet{opacity:0;transition:opacity .6s .6s}
.route.error .packet{opacity:0}
/* the single page-load sequence: the track draws, then the stops arrive */
.track{animation:drawline 1.1s var(--ease) both}
.stops li{animation:arrive .6s var(--ease) both}
.stops li:nth-child(1){animation-delay:.15s}.stops li:nth-child(2){animation-delay:.3s}.stops li:nth-child(3){animation-delay:.45s}.stops li:nth-child(4){animation-delay:.6s}.stops li:nth-child(5){animation-delay:.75s}
@keyframes drawline{from{transform:scaleX(0)}}
@keyframes arrive{from{opacity:0;transform:translateY(8px)}}
@keyframes travel{0%{left:0;opacity:0}10%{opacity:1}85%{opacity:1}100%{left:100%;opacity:0}}
@keyframes ping{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 55%,transparent)}100%{box-shadow:0 0 0 12px transparent}}

/* ---------- layout ---------- */
.layout{max-width:1180px;margin:26px auto 0;padding:0 clamp(16px,4vw,40px);display:grid;grid-template-columns:300px 1fr;gap:clamp(20px,3vw,44px);align-items:start}
@media(max-width:860px){.layout{grid-template-columns:1fr}}
.side{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;position:sticky;top:16px}
@media(max-width:860px){.side{position:static}}
h2{font:600 15px/1.3 var(--display);letter-spacing:.01em}
.side h2{margin-bottom:12px}

.acc{display:flex;align-items:center;gap:11px;padding:10px 10px 10px 13px;border-radius:10px;cursor:pointer;position:relative;transition:background .2s;margin-bottom:4px}
.acc:hover{background:var(--accent-soft)}
.acc::before{content:"";position:absolute;left:0;top:10px;bottom:10px;width:3px;border-radius:3px;background:var(--accent);transform:scaleY(0);transition:transform .3s var(--ease)}
.acc.sel{background:var(--accent-soft)}
.acc.sel::before{transform:scaleY(1)}
.acc img{width:34px;height:34px;border-radius:50%;background:var(--line)}
.acc .meta{min-width:0}.acc b{display:block;font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis}
.acc small{color:var(--muted);font-size:12.5px}
.acc .x{margin-left:auto;border:0;background:none;color:var(--muted);font-size:20px;line-height:1;width:28px;height:28px;border-radius:8px;cursor:pointer}
.acc .x:hover{color:var(--err);background:color-mix(in srgb,var(--err) 12%,transparent)}
.empty{color:var(--muted);font-size:13.5px;padding:4px 2px 8px}
.add{margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
.hint{font-size:12.5px;color:var(--muted);margin-top:10px;line-height:1.5}

.main section{padding:4px 0 26px}
.main section+section{border-top:1px solid var(--line);padding-top:24px}
.sh{display:flex;align-items:center;gap:11px;margin-bottom:14px}
.num{width:24px;height:24px;border-radius:50%;border:1.5px solid var(--accent);display:grid;place-items:center;font:600 12px var(--display);color:var(--accent-2)}
label.l{display:block;font-size:13px;font-weight:500;color:var(--muted);margin:14px 0 6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:620px){.grid2{grid-template-columns:1fr}}
.field{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--field);color:var(--text);font:inherit;transition:border-color .2s,box-shadow .2s}
.field::placeholder{color:color-mix(in srgb,var(--muted) 70%,transparent)}
.field:focus{outline:none;border-color:var(--accent-2);box-shadow:0 0 0 3px var(--accent-soft)}
select.field{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none' stroke='%2393a3bd' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M1 1.5l5 5 5-5'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 13px center;padding-right:34px}
.field.skel{background-image:linear-gradient(90deg,transparent,color-mix(in srgb,var(--accent) 18%,transparent),transparent);background-size:200% 100%;animation:shimmer 1.2s linear infinite}
@keyframes shimmer{from{background-position:200% 0}to{background-position:-200% 0}}
.row{display:flex;gap:8px}.row .field{flex:1}

.btn{font:600 14.5px var(--body);border:0;border-radius:8px;padding:10px 16px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:8px;background:var(--accent);color:var(--on-accent);transition:transform .15s,background .2s,box-shadow .2s,opacity .2s}
.btn:hover{background:color-mix(in srgb,var(--accent) 86%,#fff);box-shadow:0 6px 20px -8px var(--accent)}
.btn:active{transform:scale(.98)}
.btn:disabled{opacity:.55;cursor:not-allowed;box-shadow:none}
.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.btn.ghost:hover{border-color:var(--accent);background:var(--accent-soft);box-shadow:none}
.btn.wide{width:100%}
.btn.loading::before{content:"";width:15px;height:15px;border:2px solid color-mix(in srgb,currentColor 35%,transparent);border-top-color:currentColor;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.drop{margin-top:10px;border:1.5px dashed var(--line);border-radius:10px;padding:16px;text-align:center;color:var(--muted);font-size:13.5px;transition:border-color .2s,background .2s}
.drop.over{border-color:var(--accent-2);background:var(--accent-soft);color:var(--text)}
.drop a{font-weight:600}

.scan{margin-top:12px;animation:fadein .35s var(--ease)}
@keyframes fadein{from{opacity:0;transform:translateY(6px)}}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stat{padding:12px 14px;border-radius:10px;background:var(--panel);border:1px solid var(--line)}
.stat b{display:block;font:700 24px/1.1 var(--display);font-variant-numeric:tabular-nums}
.stat span{font-size:12.5px;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{font:12px var(--mono);padding:4px 10px;border-radius:99px;background:var(--accent-soft);border:1px solid color-mix(in srgb,var(--accent) 35%,transparent);animation:pop .35s var(--ease) both;animation-delay:calc(var(--i)*22ms)}
@keyframes pop{from{opacity:0;transform:scale(.8)}}
.note{margin-top:10px;padding:10px 12px;border-radius:8px;font-size:13.5px;border:1px solid}
.note.bad{background:color-mix(in srgb,var(--err) 10%,transparent);border-color:color-mix(in srgb,var(--err) 45%,transparent)}
.note.info{background:color-mix(in srgb,var(--warn) 12%,transparent);border-color:color-mix(in srgb,var(--warn) 45%,transparent)}

.opt{display:flex;gap:13px;align-items:flex-start;padding:13px 14px;border:1px solid var(--line);border-radius:10px;margin-top:10px;cursor:pointer;transition:border-color .2s}
.opt:hover{border-color:var(--accent)}
.opt b{display:block;font-size:14px;font-weight:600}.opt span{font-size:13px;color:var(--muted)}
.sw{appearance:none;flex:none;width:40px;height:23px;border-radius:99px;background:var(--line);position:relative;cursor:pointer;transition:background .25s;margin-top:2px}
.sw::after{content:"";position:absolute;top:3px;left:3px;width:17px;height:17px;border-radius:50%;background:#fff;transition:transform .3s var(--ease);box-shadow:0 1px 4px rgba(0,0,0,.35)}
.sw:checked{background:var(--accent)}.sw:checked::after{transform:translateX(17px)}

.err{margin-top:12px;padding:10px 12px;border-radius:8px;background:color-mix(in srgb,var(--err) 10%,transparent);border:1px solid color-mix(in srgb,var(--err) 45%,transparent);color:var(--err);font-size:13.5px;white-space:pre-wrap;animation:fadein .3s var(--ease)}
.err:empty{display:none}
.push-wrap{padding-bottom:6px}
.btn.big{width:100%;padding:14px;font-size:16px}
.btn.big svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}

.result{margin-top:16px;position:relative}
.ok-box,.bad-box{display:flex;align-items:center;gap:14px;padding:16px;border-radius:12px;border:1px solid;animation:fadein .45s var(--ease)}
.ok-box{border-color:color-mix(in srgb,var(--ok) 45%,transparent);background:color-mix(in srgb,var(--ok) 9%,transparent)}
.bad-box{border-color:color-mix(in srgb,var(--err) 45%,transparent);background:color-mix(in srgb,var(--err) 9%,transparent)}
.ok-box svg{width:44px;height:44px;flex:none}
.ok-box circle{stroke:var(--ok);stroke-width:3;fill:none;stroke-dasharray:166;stroke-dashoffset:166;animation:draw .7s var(--ease) forwards}
.ok-box path{stroke:var(--ok);stroke-width:4;fill:none;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:48;stroke-dashoffset:48;animation:draw .45s .5s var(--ease) forwards}
@keyframes draw{to{stroke-dashoffset:0}}
.ok-box .t{flex:1}.ok-box b,.bad-box b{display:block;font:600 16px var(--display)}
.ok-box small,.bad-box small{color:var(--muted);font-size:13px}
.burst{position:absolute;left:36px;top:50%;pointer-events:none}
.burst i{position:absolute;width:7px;height:7px;border-radius:2px;animation:fly .9s var(--ease) forwards}
@keyframes fly{to{transform:translate(var(--x),var(--y)) rotate(var(--r));opacity:0}}

.term{border-radius:10px;overflow:hidden;border:1px solid var(--line);background:var(--term);margin-top:14px;animation:fadein .35s var(--ease)}
.term-h{display:flex;align-items:center;gap:6px;padding:8px 12px;border-bottom:1px solid rgba(255,255,255,.07)}
.term-h i{width:10px;height:10px;border-radius:50%}
.term-h i:nth-child(1){background:#ff5f56}.term-h i:nth-child(2){background:#ffbd2e}.term-h i:nth-child(3){background:#27c93f}
.term-h span{margin-left:8px;font:12px var(--mono);color:#8d8497;flex:1}
.term-h button{font:12px var(--body);background:transparent;border:1px solid rgba(255,255,255,.15);color:#b9afc4;border-radius:6px;padding:3px 10px;cursor:pointer}
.term-h button:hover{color:#fff;border-color:rgba(255,255,255,.4)}
pre{padding:14px;max-height:300px;overflow:auto;font:12.5px/1.65 var(--mono);color:var(--termt);white-space:pre-wrap;word-break:break-word}

footer{max-width:1180px;margin:30px auto 30px;padding:20px clamp(16px,4vw,40px) 0;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:12px 24px;justify-content:space-between;align-items:center;color:var(--muted);font-size:13.5px}
.fbrand p{margin-top:8px;font-size:13.5px}
.wm{display:inline-block;border-radius:10px;padding:6px 10px;margin-left:-10px}
html[data-theme=dark] .wm{background:#eef2f9;margin-left:0}
.wm img{height:60px;width:auto;display:block}
.flinks{display:flex;gap:8px;flex-wrap:wrap}
.flinks a{display:inline-flex;align-items:center;gap:8px;padding:7px 12px;border:1px solid var(--line);border-radius:8px;color:var(--text);text-decoration:none;font-size:13.5px;transition:border-color .2s,background .2s}
.flinks a:hover{border-color:var(--accent);background:var(--accent-soft)}
.flinks svg{width:16px;height:16px;fill:currentColor}
.flinks svg.s{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
@media(prefers-reduced-motion:reduce){*{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}.route.idle .packet{animation:none}}
</style></head><body>

<header class="nav">
  <a class="brand" href="/" aria-label="__BRAND_NAME__ home">
    <span class="logo"><img src="__LOGO_MARK__" alt=""></span>
    <div><b>__BRAND_NAME__</b><small>by __AUTHOR__</small></div>
  </a>
  <div class="nav-r">
    <a class="icon-btn" href="__LINKEDIN__" target="_blank" rel="noopener" aria-label="__AUTHOR__ on LinkedIn" title="LinkedIn"><svg viewBox="0 0 24 24"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg></a>
    <a class="icon-btn" href="__GITHUB__" target="_blank" rel="noopener" aria-label="__AUTHOR__ on GitHub" title="GitHub"><svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg></a>
    <button class="icon-btn" id="theme" aria-label="Switch between light and dark theme" title="Switch theme">
      <svg class="sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
      <svg class="moon" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
    </button>
  </div>
</header>

<section class="hero">
  <h1>__TAGLINE__</h1>
  <p>Runs on your computer. Your tokens stay here, and your modules go to GitHub and nowhere else.</p>
  <div class="route idle" id="route" style="--p:0" role="status" aria-live="polite" aria-label="Push progress">
    <div class="track"><div class="fill"></div><span class="packet"></span></div>
    <ol class="stops">
      <li><i></i><span>Connect</span></li>
      <li><i></i><span>Clone</span></li>
      <li><i></i><span>Copy files</span></li>
      <li><i></i><span>Commit</span></li>
      <li><i></i><span>Push</span></li>
    </ol>
  </div>
</section>

<div class="layout">
  <aside class="side">
    <h2>Accounts</h2>
    <div id="accounts"></div>
    <div class="add">
      <label class="l" for="tok" style="margin-top:0">GitHub token</label>
      <input class="field" type="password" id="tok" placeholder="ghp_... or github_pat_..." autocomplete="off">
      <label class="l" for="lbl">Label (optional)</label>
      <input class="field" type="text" id="lbl" placeholder="Work, Client X, ...">
      <button class="btn wide" id="addBtn" style="margin-top:12px">Add account</button>
      <div class="err" id="addErr"></div>
      <p class="hint">We read your username from the token. Create a classic token with the <code>repo</code> scope
        (<a href="https://github.com/settings/tokens/new?scopes=repo&description=__BRAND_NAME_URL__" target="_blank" rel="noopener">create one</a>),
        or a fine-grained token with Contents: read and write. Add as many as you need.</p>
    </div>
  </aside>

  <main class="main">
    <section>
      <div class="sh"><span class="num">1</span><h2>Destination</h2></div>
      <div class="grid2">
        <div><label class="l" for="repo" style="margin-top:0">Repository</label>
          <input class="field" type="text" id="repo" list="repos" placeholder="owner/name" autocomplete="off">
          <datalist id="repos"></datalist></div>
        <div><label class="l" for="branchSel" style="margin-top:0">Branch</label>
          <select class="field" id="branchSel"></select>
          <input class="field" type="text" id="branch" placeholder="New branch name" autocomplete="off" hidden style="margin-top:8px"></div>
      </div>
      <label class="l" for="msg">Commit message</label>
      <input class="field" type="text" id="msg" placeholder="Add or update modules">
    </section>

    <section>
      <div class="sh"><span class="num">2</span><h2>Modules</h2></div>
      <label class="l" for="path" style="margin-top:0">Folder</label>
      <div class="row">
        <input class="field" type="text" id="path" placeholder="/home/you/odoo/custom_addons">
        <button class="btn ghost" id="browse">Browse</button>
        <button class="btn ghost" id="scanBtn">Scan</button>
      </div>
      <div class="drop" id="drop">Drop a .zip of your modules here, or <a href="#" id="pickzip">choose a zip</a>
        <input type="file" id="zip" accept=".zip" hidden></div>
      <div class="scan" id="scan" hidden></div>
    </section>

    <section>
      <div class="sh"><span class="num">3</span><h2>Options</h2></div>
      <label class="l" for="subdir" style="margin-top:0">Subfolder in the repository</label>
      <input class="field" type="text" id="subdir" placeholder="Leave empty to use the repository root">
      <label class="opt"><input class="sw" type="checkbox" id="clean" checked>
        <div><b>Skip build junk</b><span>.git, __pycache__, *.pyc, node_modules, .idea and .vscode stay out of the repository.</span></div></label>
      <label class="opt"><input class="sw" type="checkbox" id="mirror">
        <div><b>Mirror my folder</b><span>Delete repository files that are not in your folder. This also removes the README unless your folder has one.</span></div></label>
    </section>

    <section class="push-wrap">
      <button class="btn big" id="push">
        <svg viewBox="0 0 24 24"><path d="M12 16V4M6.5 9.5 12 4l5.5 5.5M5 20h14"/></svg>
        <span>Push to GitHub</span></button>
      <div class="err" id="pushErr"></div>
      <div class="result" id="status"></div>
      <div class="term" id="term" hidden>
        <div class="term-h"><i></i><i></i><i></i><span>push log</span><button id="copyLog">Copy log</button></div>
        <pre id="log"></pre>
      </div>
    </section>
  </main>
</div>

<footer>
  <div class="fbrand">
    <span class="wm"><img src="__WORDMARK__" alt="__AUTHOR__, Python and Odoo developer"></span>
    <p>Need help with an Odoo project? Get in touch.</p>
  </div>
  <div class="flinks">
    <a href="__LINKEDIN__" target="_blank" rel="noopener"><svg viewBox="0 0 24 24"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>LinkedIn</a>
    <a href="__GITHUB__" target="_blank" rel="noopener"><svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>GitHub</a>
    <a href="mailto:__EMAIL__"><svg class="s" viewBox="0 0 24 24"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M22 6l-10 7L2 6"/></svg>__EMAIL__</a>
    <a href="tel:__PHONE_TEL__"><svg class="s" viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72 12.84 12.84 0 00.7 2.81 2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45 12.84 12.84 0 002.81.7A2 2 0 0122 16.92z"/></svg>__PHONE__</a>
  </div>
</footer>

<script>
const TOKEN="__APP_TOKEN__";
const $=s=>document.querySelector(s);
const reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
let profiles=[],cur=localStorage.getItem("cur")||"",repos=[],branchList=[];

async function api(path,opt={}){
  opt.headers=Object.assign({"X-App-Token":TOKEN},opt.headers||{});
  if(opt.json!==undefined){opt.body=JSON.stringify(opt.json);opt.method=opt.method||"POST";opt.headers["Content-Type"]="application/json";}
  const r=await fetch(path,opt);const d=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(d.error||r.statusText);return d;
}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const busy=(b,on)=>{b.classList.toggle("loading",on);b.disabled=on};
const lastKey=()=>"last:"+cur;
const getLast=()=>{try{return JSON.parse(localStorage.getItem(lastKey())||"{}")}catch(e){return{}}};
function saveLast(){if(cur)localStorage.setItem(lastKey(),JSON.stringify({repo:$("#repo").value,branch:$("#branch").value,path:$("#path").value,msg:$("#msg").value,subdir:$("#subdir").value}))}

/* theme */
$("#theme").onclick=()=>{
  const t=document.documentElement.dataset.theme==="dark"?"light":"dark";
  document.documentElement.dataset.theme=t;try{localStorage.setItem("theme",t)}catch(e){}
};

/* route: shows the real push stages */
const route=$("#route"),stops=[...route.querySelectorAll("li")];
const STAGES=["git clone","copy files","git commit","git push"];
const stageOf=log=>{let n=0;STAGES.forEach((k,i)=>{if(log.includes("$ "+k))n=i+1});return n};
function setRoute(state,n){
  route.className="route "+state;
  route.style.setProperty("--p",state==="done"?1:n/4);
  stops.forEach((li,i)=>{
    li.className=state==="done"||i<n?"done":(i===n&&state!=="idle")?(state==="error"?"error":"active"):"";
  });
}

/* accounts */
async function loadProfiles(){
  profiles=await api("/api/profiles");
  if(!profiles.find(p=>p.id===cur))cur=profiles[0]?profiles[0].id:"";
  renderAccounts();if(cur)selectProfile(cur,true);else renderBranches("");
}
function renderAccounts(){
  const el=$("#accounts");
  if(!profiles.length){el.innerHTML='<p class="empty">No accounts yet. Paste a GitHub token below to add one.</p>';return}
  el.innerHTML=profiles.map(p=>`<div class="acc ${p.id===cur?"sel":""}" data-id="${p.id}">
    <img src="${esc(p.avatar)}" alt=""><div class="meta"><b>${esc(p.username)}</b><small>${esc(p.label||p.name)}</small></div>
    <button class="x" data-del="${p.id}" title="Remove account" aria-label="Remove ${esc(p.username)}">&times;</button></div>`).join("");
  el.querySelectorAll(".acc").forEach(a=>a.onclick=e=>{
    if(e.target.dataset.del){removeAcc(e.target.dataset.del);return}selectProfile(a.dataset.id)});
}
async function removeAcc(id){
  if(!confirm("Remove this saved token from this computer?"))return;
  await api("/api/profiles/"+id,{method:"DELETE"});await loadProfiles();
}
async function selectProfile(id,skipRender){
  cur=id;localStorage.setItem("cur",id);if(!skipRender)renderAccounts();
  const l=getLast();
  $("#repo").value=l.repo||"";$("#branch").value=l.branch||"";$("#path").value=l.path||"";
  $("#msg").value=l.msg||"";$("#subdir").value=l.subdir||"";
  $("#pushErr").textContent="";
  const rf=$("#repo");rf.placeholder="Loading repositories...";rf.classList.add("skel");$("#repos").innerHTML="";repos=[];
  try{
    repos=await api("/api/repos?profile="+cur);
    $("#repos").innerHTML=repos.map(r=>`<option value="${esc(r.name)}">${r.private?"private":"public"}</option>`).join("");
    rf.placeholder="Search "+repos.length+" repositories";
    if(rf.value)loadBranches();else renderBranches("");
  }catch(e){rf.placeholder="owner/name";$("#pushErr").textContent=e.message;renderBranches("")}
  rf.classList.remove("skel");
}

/* branches */
function renderBranches(want){
  const sel=$("#branchSel");
  sel.innerHTML=branchList.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join("")
    +'<option value="__new__">+ New branch...</option>';
  if(want&&branchList.includes(want)){sel.value=want;$("#branch").hidden=true}
  else{sel.value="__new__";$("#branch").hidden=false}
  $("#branch").value=want||"";
}
async function loadBranches(){
  const v=$("#repo").value.trim();branchList=[];
  if(!/^[\w.-]+\/[\w.-]+$/.test(v)){renderBranches("");return}
  let want=$("#branch").value.trim();
  try{
    branchList=await api("/api/branches?profile="+cur+"&repo="+encodeURIComponent(v));
    if(!want){const r=repos.find(r=>r.name===v);want=(r&&r.branch)||branchList[0]||"main"}
  }catch(e){$("#pushErr").textContent="Could not load branches: "+e.message}
  renderBranches(want);
}
$("#branchSel").addEventListener("change",()=>{
  const s=$("#branchSel").value;
  if(s==="__new__"){$("#branch").hidden=false;$("#branch").value="";$("#branch").focus()}
  else{$("#branch").hidden=true;$("#branch").value=s}
  saveLast();
});
$("#repo").addEventListener("focus",e=>{e.target.dataset.old=e.target.value;e.target.value=""});
$("#repo").addEventListener("blur",e=>{if(!e.target.value)e.target.value=e.target.dataset.old||""});
$("#repo").addEventListener("change",()=>{
  const r=$("#repo");if(r.value!==r.dataset.old)$("#branch").value="";
  loadBranches();saveLast();
});

/* add account */
$("#addBtn").onclick=async()=>{
  const b=$("#addBtn");$("#addErr").textContent="";busy(b,true);
  try{
    const p=await api("/api/profiles",{json:{token:$("#tok").value,label:$("#lbl").value}});
    $("#tok").value="";$("#lbl").value="";cur=p.id;await loadProfiles();
  }catch(e){$("#addErr").textContent=e.message}
  busy(b,false);
};

/* source folder */
$("#browse").onclick=async()=>{
  try{const r=await api("/api/browse",{json:{}});if(r.path){$("#path").value=r.path;doScan()}}
  catch(e){$("#pushErr").textContent=e.message}
};
function countUp(el,n){
  if(reduce||n<2){el.textContent=n.toLocaleString();return}
  const t0=performance.now(),d=650;
  (function f(t){const k=Math.min(1,(t-t0)/d),e=1-Math.pow(1-k,3);el.textContent=Math.round(n*e).toLocaleString();if(k<1)requestAnimationFrame(f)})(t0);
}
async function doScan(){
  const box=$("#scan"),b=$("#scanBtn");box.hidden=false;box.innerHTML='<p class="hint">Scanning...</p>';busy(b,true);
  try{
    const s=await api("/api/scan",{json:{path:$("#path").value}});
    let h=`<div class="stats"><div class="stat"><b data-n="${s.modules.length}">0</b><span>Odoo modules</span></div>
      <div class="stat"><b data-n="${s.files}">0</b><span>files</span></div>
      <div class="stat"><b>${esc(s.size)}</b><span>total size</span></div></div>`;
    if(s.modules.length){
      h+='<div class="chips">'+s.modules.slice(0,30).map((m,i)=>`<span class="chip" style="--i:${i}">${esc(m)}</span>`).join("")
        +(s.modules.length>30?`<span class="chip" style="--i:30">+${s.modules.length-30} more</span>`:"")+"</div>";
    }else h+='<div class="note info">No __manifest__.py found. Check that this is the folder that contains your modules.</div>';
    if(s.big.length)h+=`<div class="note bad"><b>Over GitHub's 100 MB file limit:</b> ${s.big.map(esc).join(", ")}. The push will fail until these are removed.</div>`;
    box.innerHTML=h;box.querySelectorAll("[data-n]").forEach(el=>countUp(el,+el.dataset.n));saveLast();
  }catch(e){box.innerHTML=`<div class="note bad">${esc(e.message)}</div>`}
  busy(b,false);
}
$("#scanBtn").onclick=doScan;

async function uploadZip(f){
  if(!f)return;const box=$("#scan");box.hidden=false;box.innerHTML=`<p class="hint">Extracting ${esc(f.name)}...</p>`;
  try{
    const r=await api("/api/upload?name="+encodeURIComponent(f.name),{method:"POST",body:f,headers:{"Content-Type":"application/zip"}});
    $("#path").value=r.path;await doScan();
  }catch(e){box.innerHTML=`<div class="note bad">${esc(e.message)}</div>`}
}
$("#pickzip").onclick=e=>{e.preventDefault();$("#zip").click()};
$("#zip").onchange=e=>uploadZip(e.target.files[0]);
const drop=$("#drop");
["dragover","dragenter"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("over")}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("over")}));
drop.addEventListener("drop",e=>uploadZip(e.dataTransfer.files[0]));

/* push */
function burst(host){
  if(reduce)return;const b=document.createElement("div");b.className="burst";
  const colors=["#2fbf84","#6aa8ff","#f5c84b","#9ec1ff","#ff7a90"];
  for(let i=0;i<22;i++){
    const s=document.createElement("i"),a=Math.random()*Math.PI*2,d=50+Math.random()*80;
    s.style.cssText=`background:${colors[i%5]};--x:${Math.cos(a)*d}px;--y:${Math.sin(a)*d}px;--r:${Math.random()*360}deg`;b.appendChild(s);
  }
  host.appendChild(b);setTimeout(()=>b.remove(),1000);
}
let timer=null;
$("#push").onclick=async()=>{
  const btn=$("#push");
  $("#pushErr").textContent="";$("#status").innerHTML="";
  if(!cur){$("#pushErr").textContent="Add a GitHub account first.";return}
  saveLast();
  try{
    const r=await api("/api/push",{json:{profile:cur,repo:$("#repo").value.trim(),branch:$("#branch").value.trim(),
      message:$("#msg").value.trim(),path:$("#path").value.trim(),subdir:$("#subdir").value,
      clean:$("#clean").checked,mirror:$("#mirror").checked}});
    busy(btn,true);btn.querySelector("span").textContent="Pushing...";
    $("#term").hidden=false;$("#log").textContent="";setRoute("running",0);
    route.scrollIntoView({behavior:reduce?"auto":"smooth",block:"center"});
    clearInterval(timer);
    timer=setInterval(async()=>{
      try{
        const j=await api("/api/job?id="+r.id),pre=$("#log");
        const atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
        pre.textContent=j.log;if(atEnd)pre.scrollTop=pre.scrollHeight;
        if(j.status==="running"){setRoute("running",stageOf(j.log));return}
        clearInterval(timer);busy(btn,false);btn.querySelector("span").textContent="Push to GitHub";
        const st=$("#status");
        if(j.status==="done"){
          setRoute("done",4);
          st.innerHTML=`<div class="ok-box"><svg viewBox="0 0 52 52"><circle cx="26" cy="26" r="25"/><path d="M14 27l8 8 16-16"/></svg>
            <div class="t"><b>Pushed to GitHub</b><small>${esc($("#repo").value)} on ${esc($("#branch").value)}</small></div>
            <a class="btn" href="${esc(j.url)}" target="_blank" rel="noopener" style="text-decoration:none">Open on GitHub</a></div>`;
          burst(st);
        }else{
          setRoute("error",stageOf(j.log));
          st.innerHTML=`<div class="bad-box"><div class="t"><b>The push failed</b><small>The last lines of the log below say why.</small></div></div>`;
        }
      }catch(e){clearInterval(timer);busy(btn,false);btn.querySelector("span").textContent="Push to GitHub";$("#pushErr").textContent=e.message}
    },700);
  }catch(e){$("#pushErr").textContent=e.message}
};
$("#copyLog").onclick=()=>{
  const b=$("#copyLog");
  navigator.clipboard.writeText($("#log").textContent).then(()=>{b.textContent="Copied";setTimeout(()=>b.textContent="Copy log",1400)});
};
["#branch","#msg","#path","#subdir"].forEach(s=>$(s).addEventListener("change",saveLast));
loadProfiles();
</script></body></html>
"""


# ============================ LOGO ASSETS =============================
# Embedded images (monogram, wordmark, favicon). Replace the base64 strings
# to change the logo, or ask for help regenerating them from a new PNG.
LOGO_MARK_B64 = "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"
WORDMARK_B64 = "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"
FAVICON_B64 = "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"


# -------------------------------------------------------------------- main
def install_launcher():
    d = Path.home() / ".local/share/applications"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "odoo-pusher.desktop"
    f.write_text(f"[Desktop Entry]\nType=Application\nName={BRAND['name']}\n"
                 f"Comment=Push Odoo modules to GitHub\nExec=python3 {Path(__file__).resolve()}\n"
                 f"Terminal=false\nCategories=Development;\n")
    print("Launcher installed. Search for '" + BRAND['name'] + "' in your applications menu.")


def main():
    if "--install" in sys.argv:
        return install_launcher()
    for cmd in ("git", "rsync"):
        if not shutil.which(cmd):
            print(f"Missing '{cmd}'. Run: sudo apt install git rsync")
            return 1
    server, port = None, PORT
    for port in range(PORT, PORT + 15):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not server:
        print("No free port found.")
        return 1
    url = f"http://127.0.0.1:{port}/"
    print(f"{BRAND['name']} running at {url}  (Ctrl+C to stop)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
