import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "ui"
CONFIG_FILE = APP_DIR / "hub.config.json"
EXAMPLE_CONFIG_FILE = APP_DIR / "hub.config.example.json"
COMMENTS_FILE = APP_DIR / "comments.json"

SKIP_SYSTEM_DIRS = {
    ".agents", ".git", ".github", ".vscode", ".idea", "backups",
    "graphify-out", "knowledge", "node_modules", "__pycache__",
}

app = FastAPI(title="Antigravity Hub — Local Apps & Skills Launcher")
PROCESS_LOCK = threading.Lock()
RUNNING_PROCS: Dict[str, Dict[str, Any]] = {}


def load_config() -> Dict[str, Any]:
    target = CONFIG_FILE if CONFIG_FILE.exists() else EXAMPLE_CONFIG_FILE
    if target.exists():
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "port": 8787,
        "host": "127.0.0.1",
        "workspace_roots": ["auto"],
        "skills_dir": "~/.gemini/config/skills",
        "master_skills_dir": "",
        "graphify_json": "../graphify-out/graph.json",
        "graphify_html": "../graphify-out/graph.html",
        "excluded_names": [],
        "custom_apps": [],
    }


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def discover_system_roots() -> List[Path]:
    """Dynamically find active dev/project folders across any user's mounted drives without heavy crawling."""
    candidates: List[Path] = []
    seen: set[str] = set()
    common_names = ("Antigravity", "Projects", "Dev", "Repos", "Code", "Work", "GitHub")

    def add_if_valid(p: Path) -> None:
        try:
            if p.exists() and p.is_dir():
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    candidates.append(p.resolve())
        except OSError:
            pass

    add_if_valid(APP_DIR.parent)
    home = Path.home()
    for sub in ("Antigravity", "Projects", "Code", "Dev", "Documents/GitHub", "Documents/Projects"):
        add_if_valid(home / sub)

    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            if letter in ("A", "B"):
                continue
            drive = Path(f"{letter}:\\")
            if drive.exists():
                for name in common_names:
                    add_if_valid(drive / name)
    elif sys.platform == "darwin":
        vols = Path("/Volumes")
        if vols.exists():
            for vol in vols.iterdir():
                if vol.is_dir():
                    for name in common_names:
                        add_if_valid(vol / name)
    else:
        for base in (Path("/mnt"), Path("/media")):
            if base.exists():
                for sub in base.iterdir():
                    if sub.is_dir():
                        for name in common_names:
                            add_if_valid(sub / name)

    return candidates


def resolve_workspace_roots(cfg: Dict[str, Any]) -> List[Path]:
    raw_roots = cfg.get("workspace_roots", ["auto"])
    resolved: List[Path] = []
    seen: set[str] = set()
    for raw in raw_roots:
        if str(raw).strip().lower() == "auto":
            for p in discover_system_roots():
                k = str(p).lower()
                if k not in seen:
                    seen.add(k)
                    resolved.append(p)
        else:
            p = resolve_path(str(raw))
            k = str(p).lower()
            if p.exists() and p.is_dir() and k not in seen:
                seen.add(k)
                resolved.append(p)
    return resolved


def resolve_path(raw: str) -> Path:
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = (APP_DIR / p).resolve()
    return p


def load_comments() -> Dict[str, str]:
    if COMMENTS_FILE.exists():
        try:
            return json.loads(COMMENTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_comments(data: Dict[str, str]) -> None:
    COMMENTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_graphify_index(graph_json: Path) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    if not graph_json.exists():
        return index
    try:
        raw = json.loads(graph_json.read_text(encoding="utf-8"))
        nodes = raw.get("nodes", [])
        links = raw.get("links", raw.get("edges", []))
        deg: Dict[str, int] = {}
        for lk in links:
            s, t = str(lk.get("source", "")), str(lk.get("target", ""))
            deg[s] = deg.get(s, 0) + 1
            deg[t] = deg.get(t, 0) + 1
        for nd in nodes:
            nid = str(nd.get("id", ""))
            label = str(nd.get("label", nid)).lower()
            entry = {"community": nd.get("community"), "connections": deg.get(nid, 0), "label": nd.get("label", nid)}
            index[label] = entry
            slug = re.sub(r"[^a-z0-9]+", "", label)
            if slug:
                index[slug] = entry
    except Exception:
        pass
    return index


def find_graphify_meta(graph_idx: Dict[str, Dict[str, Any]], name: str) -> Dict[str, Any]:
    k1 = name.lower()
    k2 = re.sub(r"[^a-z0-9]+", "", k1)
    return graph_idx.get(k1) or graph_idx.get(k2) or {}


def parse_skill_frontmatter(skill_md: Path) -> Dict[str, str]:
    info = {"name": skill_md.parent.name, "description": ""}
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")[:3000]
        m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
        if m:
            block = m.group(1)
            nm = re.search(r"^name:\s*['\"]?(.+?)['\"]?\s*$", block, re.M)
            if nm:
                info["name"] = nm.group(1).strip()
            dm = re.search(r"^description:\s*(?:>-|\||>)?\s*\n((?:\s+.*\n?)+)", block, re.M)
            if dm:
                info["description"] = " ".join(line.strip() for line in dm.group(1).splitlines())[:220]
            else:
                dm2 = re.search(r"^description:\s*['\"]?(.+?)['\"]?\s*$", block, re.M)
                if dm2:
                    info["description"] = dm2.group(1).strip()[:220]
    except Exception:
        pass
    return info


def quick_scan_folder(folder: Path, max_files: int = 12, max_depth: int = 2) -> tuple[bool, list[str]]:
    skip_dirs = {"node_modules", ".git", ".venv", "venv", ".next", "dist", "build", "__pycache__", "obj", "bin", "target", ".turbo", ".yarn"}
    code_markers = {"package.json", "pyproject.toml", "requirements.txt", "pubspec.yaml", "app.py", "main.py", "server.py", "index.html"}
    code_exts = {".exe", ".bat", ".sh", ".csproj", ".py", ".ts", ".js", ".tsx", ".jsx", ".html"}
    seen_names: list[str] = []
    has_code = False

    def walk(cur: Path, depth: int) -> None:
        nonlocal has_code
        if depth > max_depth or len(seen_names) >= max_files:
            return
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in skip_dirs and depth < max_depth:
                            walk(Path(entry.path), depth + 1)
                    elif entry.is_file(follow_symlinks=False):
                        seen_names.append(entry.name)
                        if entry.name in code_markers or Path(entry.name).suffix.lower() in code_exts:
                            has_code = True
                        if len(seen_names) >= max_files and has_code:
                            return
        except OSError:
            pass

    walk(folder, 0)
    return has_code, seen_names


_ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_API_VAR_DOC_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]{2,40}_(?:API_KEY|TOKEN|SECRET|CLIENT_SECRET|BOT_TOKEN)|"
    r"(?:OPENAI|GEMINI|GOOGLE|ANTHROPIC|ELEVENLABS|MUAPI|REPLICATE|GROQ|OPENROUTER|DEEPSEEK|COHERE|PERPLEXITY|STABILITY|RUNWAY|LUMA|KLING|PIKA|MINIMAX|SUNO|HUGGINGFACE|HF|FAL)_[A-Z0-9_]*(?:KEY|TOKEN))\b"
)
_SECRET_KEY_SUFFIX_RE = re.compile(r"(?:API_KEY|_KEY|_SECRET|_TOKEN|_PASSWORD|_AUTH|CLIENT_SECRET|WEBHOOK_URL|DATABASE_URL|DSN)$")
_PLACEHOLDER_RE = re.compile(r"^(?:your[-_].*|my[-_].*|replace[-_]?me.*|changeme.*|example.*|dummy.*|test[-_]?key.*|sk-x+|x{4,}|<[^>]+>|\$\{[^}]+\})$", re.IGNORECASE)
_SKIP_DOC_KEYS = {"API_KEY", "YOUR_API_KEY", "MY_API_KEY", "EXAMPLE_API_KEY", "REPLACE_API_KEY", "BEARER_TOKEN", "ACCESS_TOKEN", "REFRESH_TOKEN"}


def _is_meaningful_secret(raw_val: str) -> bool:
    val = raw_val.strip().strip('"').strip("'").strip()
    if not val:
        return False
    return not bool(_PLACEHOLDER_RE.match(val))


def _read_env_file_raw(env_path: Path) -> Dict[str, str]:
    """Internal-only reader for launch env injection; never exposed in API responses."""
    out: Dict[str, str] = {}
    if not env_path.exists() or not env_path.is_file():
        return out
    try:
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = _ENV_ASSIGN_RE.match(line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip()
            if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
                val = val[1:-1]
            out[key] = val
    except Exception:
        pass
    return out


def _read_env_presence(env_path: Path) -> Dict[str, bool]:
    """Return boolean presence per key only — never return secret values."""
    return {k: _is_meaningful_secret(v) for k, v in _read_env_file_raw(env_path).items()}


def _upsert_env_file(env_path: Path, updates: Dict[str, str]) -> List[str]:
    clean_updates = {k.strip(): v.strip() for k, v in updates.items() if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k.strip()) and v.strip()}
    if not clean_updates:
        return []
    lines: List[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    remaining = dict(clean_updates)
    new_lines: List[str] = []
    for line in lines:
        m = _ENV_ASSIGN_RE.match(line)
        if m and m.group(1) in remaining:
            k = m.group(1)
            v = remaining.pop(k).replace("\r", "").replace("\n", "")
            quoted = f'"{v}"' if any(ch.isspace() or ch == "#" for ch in v) else v
            new_lines.append(f"{k}={quoted}")
        else:
            new_lines.append(line)
    for k, v in remaining.items():
        clean_v = v.replace("\r", "").replace("\n", "")
        quoted = f'"{clean_v}"' if any(ch.isspace() or ch == "#" for ch in clean_v) else clean_v
        new_lines.append(f"{k}={quoted}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return list(clean_updates.keys())


def detect_api_requirements(item_path: Path, cwd_path: Optional[Path] = None, is_skill: bool = False) -> Dict[str, Any]:
    """Inspect required env key names and return boolean configured status only."""
    declared: List[str] = []
    seen = set()

    def add_key(k: str) -> None:
        if k and k not in _SKIP_DOC_KEYS and k not in seen:
            seen.add(k)
            declared.append(k)

    search_dirs = [item_path]
    if cwd_path and cwd_path != item_path and cwd_path.exists():
        search_dirs.append(cwd_path)

    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for tmpl_name in (".env.example", ".env.template", ".env.sample"):
            tp = sdir / tmpl_name
            if tp.exists():
                for k in _read_env_presence(tp):
                    if _SECRET_KEY_SUFFIX_RE.search(k):
                        add_key(k)
        for env_name in (".env", ".env.local"):
            ep = sdir / env_name
            if ep.exists():
                for k in _read_env_presence(ep):
                    if _SECRET_KEY_SUFFIX_RE.search(k):
                        add_key(k)

    if not declared:
        doc_candidates: List[Path] = []
        if is_skill:
            doc_candidates.append(item_path / "SKILL.md")
            if (item_path / "scripts").exists():
                doc_candidates.extend(sorted((item_path / "scripts").glob("*.py"))[:4])
        else:
            doc_candidates.append(item_path / "README.md")
        for doc in doc_candidates:
            if doc.exists() and doc.is_file():
                try:
                    txt = doc.read_text(encoding="utf-8", errors="ignore")[:32768]
                    for m in _API_VAR_DOC_RE.finditer(txt):
                        add_key(m.group(1))
                except Exception:
                    pass

    if not declared:
        return {"needs_api": False, "all_configured": True, "missing_count": 0, "keys": [], "env_file": str(item_path / ".env")}

    presence: Dict[str, bool] = {}
    for k in declared:
        presence[k] = _is_meaningful_secret(os.environ.get(k, ""))

    for env_candidate in (APP_DIR / ".env", *(d / ".env.local" for d in search_dirs), *(d / ".env" for d in search_dirs)):
        for k, is_set in _read_env_presence(env_candidate).items():
            if k in presence and is_set:
                presence[k] = True

    key_entries = [{"key": k, "configured": bool(presence.get(k, False))} for k in declared]
    configured = sum(1 for entry in key_entries if entry["configured"])
    missing = len(key_entries) - configured
    return {
        "needs_api": True,
        "all_configured": missing == 0,
        "configured_count": configured,
        "missing_count": missing,
        "keys": key_entries,
        "env_file": str(item_path / ".env"),
    }


def _collect_launch_env(item_path: Optional[Path], cwd_path: Optional[Path]) -> Dict[str, str]:
    env = os.environ.copy()
    candidates = [APP_DIR / ".env"]
    if item_path and item_path.exists():
        candidates.extend([item_path / ".env", item_path / ".env.local"])
    if cwd_path and cwd_path.exists() and cwd_path != item_path:
        candidates.extend([cwd_path / ".env", cwd_path / ".env.local"])
    for ep in candidates:
        for k, v in _read_env_file_raw(ep).items():
            if _is_meaningful_secret(v):
                env[k] = v
    return env


def _is_running(item_id: str) -> tuple[bool, Optional[str]]:
    with PROCESS_LOCK:
        info = RUNNING_PROCS.get(item_id)
        if info and info["proc"].poll() is None:
            return True, info.get("url")
        return False, None


def detect_repo(folder: Path, excluded: set[str], graph_idx: Dict[str, Any], comments: Dict[str, str]) -> Optional[Dict[str, Any]]:
    name = folder.name
    if name in SKIP_SYSTEM_DIRS or name.startswith("."):
        return None

    gmeta = find_graphify_meta(graph_idx, name)
    location_tag = folder.drive.rstrip(":") or "local"

    if name in excluded:
        return {
            "id": f"stub:{name}",
            "kind": "stub",
            "name": name,
            "location": location_tag,
            "path": str(folder),
            "category": "Excluded",
            "launch_type": "none",
            "command": "",
            "status": "excluded",
            "description": "Excluded via hub.config.json.",
            "comment": comments.get(f"stub:{name}", ""),
            "graphify": gmeta,
        }

    has_code, top_files = quick_scan_folder(folder)
    if not has_code and len(top_files) <= 4:
        return {
            "id": f"stub:{name}",
            "kind": "stub",
            "name": name,
            "location": location_tag,
            "path": str(folder),
            "category": "Excluded",
            "launch_type": "none",
            "command": "",
            "status": "excluded",
            "description": f"Excluded placeholder/non-code folder ({len(top_files)} file(s)).",
            "comment": comments.get(f"stub:{name}", ""),
            "graphify": gmeta,
        }

    item_id = f"repo:{name}"
    running, run_url = _is_running(item_id)

    pkg_json = folder / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
            scripts = pkg.get("scripts", {}) or {}
            cmd = None
            pm = "bun" if (folder / "bun.lock").exists() else ("yarn" if (folder / "yarn.lock").exists() else "npm")
            for cand in ("dev", "studio", "start", "playground", "preview"):
                if cand in scripts:
                    cmd = "npm start" if (pm == "npm" and cand == "start") else f"{pm} run {cand}"
                    break
            ready = (folder / "node_modules").exists()
            # If repo has examples/index.html with no root index.html, append /examples/ automatically once port is detected
            sub_path = "/examples/" if ((folder / "examples" / "index.html").exists() and not (folder / "index.html").exists()) else ""
            if cmd:
                return {
                    "id": item_id,
                    "kind": "repo",
                    "name": name,
                    "location": location_tag,
                    "path": str(folder),
                    "category": "Web & Apps",
                    "launch_type": "web_server",
                    "command": cmd,
                    "url": (f"{run_url.rstrip('/')}{sub_path}" if (run_url and sub_path and not run_url.endswith(sub_path)) else run_url),
                    "sub_path": sub_path,
                    "status": "running" if running else ("ready" if ready else "needs_setup"),
                    "description": pkg.get("description") or f"Node/JS project ({', '.join(list(scripts.keys())[:4])}).",
                    "comment": comments.get(item_id, ""),
                    "graphify": gmeta,
                }
        except Exception:
            pass

    for py_entry in ("app.py", "main.py", "server.py"):
        if (folder / py_entry).exists():
            venv_py = folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            py_bin = f'"{venv_py}"' if venv_py.exists() else sys.executable
            return {
                "id": item_id,
                "kind": "repo",
                "name": name,
                "location": location_tag,
                "path": str(folder),
                "category": "Python & CLI",
                "launch_type": "web_server",
                "command": f"{py_bin} {py_entry}",
                "url": run_url,
                "status": "running" if running else "ready",
                "description": f"Python project ({py_entry}).",
                "comment": comments.get(item_id, ""),
                "graphify": gmeta,
            }

    if (folder / "index.html").exists():
        return {
            "id": item_id,
            "kind": "repo",
            "name": name,
            "location": location_tag,
            "path": str(folder),
            "category": "Web & Apps",
            "launch_type": "open_file",
            "command": f'"{folder / "index.html"}"',
            "url": str(folder / "index.html"),
            "status": "ready",
            "description": "Static HTML web app.",
            "comment": comments.get(item_id, ""),
            "graphify": gmeta,
        }

    return None


def scan_skills(cfg: Dict[str, Any], graph_idx: Dict[str, Any], comments: Dict[str, str]) -> List[Dict[str, Any]]:
    skills_dir = resolve_path(cfg.get("skills_dir", "~/.gemini/config/skills"))
    master_raw = cfg.get("master_skills_dir", "")
    master_dir = resolve_path(master_raw) if master_raw else skills_dir

    active_names = {d.name for d in skills_dir.iterdir()} if skills_dir.exists() else set()
    scan_root = master_dir if master_dir.exists() else skills_dir
    if not scan_root.exists():
        return []

    skills: List[Dict[str, Any]] = []
    for d in sorted(scan_root.iterdir(), key=lambda x: x.name.lower()):
        if not d.is_dir():
            continue
        sm = d / "SKILL.md"
        if not sm.exists():
            continue
        meta = parse_skill_frontmatter(sm)
        scripts = [f.name for f in (d / "scripts").glob("*") if f.is_file()] if (d / "scripts").exists() else []
        is_active = d.name in active_names
        item_id = f"skill:{d.name}"
        skills.append({
            "id": item_id,
            "kind": "skill",
            "name": meta["name"] or d.name,
            "folder": d.name,
            "location": d.drive.rstrip(":") or "local",
            "path": str(d),
            "active": is_active,
            "can_toggle": bool(master_raw and master_dir != skills_dir),
            "command": f'"{sys.executable}" scripts/{scripts[0]}' if scripts and scripts[0].endswith(".py") else "",
            "cwd": str(d),
            "scripts": scripts,
            "description": meta["description"] or f"Skill located at {d}",
            "comment": comments.get(item_id, ""),
            "graphify": find_graphify_meta(graph_idx, d.name),
        })
    return skills


def build_inventory() -> Dict[str, Any]:
    cfg = load_config()
    comments = load_comments()
    graph_json = resolve_path(cfg.get("graphify_json", "../graphify-out/graph.json"))
    graph_idx = load_graphify_index(graph_json)
    excluded = set(cfg.get("excluded_names", []))

    items_by_name: Dict[str, Dict[str, Any]] = {}
    stubs: List[Dict[str, Any]] = []

    active_roots = resolve_workspace_roots(cfg)
    for root in active_roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            detected = detect_repo(child, excluded, graph_idx, comments)
            if not detected:
                continue
            if detected["kind"] == "stub":
                stubs.append(detected)
            else:
                items_by_name[detected["name"]] = detected

    # Merge explicit custom_apps from hub.config.json
    for app_cfg in cfg.get("custom_apps", []):
        if not app_cfg.get("enabled", True):
            continue
        name = app_cfg.get("name")
        cwd = str(resolve_path(app_cfg.get("cwd", ".")))
        if not name or not Path(cwd).exists():
            continue
        item_id = f"repo:{name}"
        running, run_url = _is_running(item_id)
        items_by_name[name] = {
            "id": item_id,
            "kind": "repo",
            "name": name,
            "location": Path(cwd).drive.rstrip(":") or "local",
            "path": cwd,
            "category": app_cfg.get("category", "Web & Apps"),
            "launch_type": app_cfg.get("launch_type", "web_server"),
            "command": app_cfg.get("command", ""),
            "url": run_url or app_cfg.get("url"),
            "status": "running" if running else "ready",
            "description": app_cfg.get("description", ""),
            "comment": comments.get(item_id, ""),
            "graphify": find_graphify_meta(graph_idx, name),
        }

    repos = list(items_by_name.values())
    for item in repos:
        item["api"] = detect_api_requirements(Path(item["path"]), Path(item.get("cwd") or item["path"]), is_skill=False)
    repos.sort(key=lambda x: (0 if x["status"] == "running" else (1 if x["status"] == "ready" else 2), x["name"].lower()))
    skills = scan_skills(cfg, graph_idx, comments)
    for skill in skills:
        skill["api"] = detect_api_requirements(Path(skill["path"]), Path(skill.get("cwd") or skill["path"]), is_skill=True)

    return {
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "graphify_nodes": len(graph_idx),
        "roots": [str(r) for r in active_roots],
        "repos": repos,
        "stubs": stubs,
        "skills": skills,
    }


def _stream_proc_output(item_id: str, proc: subprocess.Popen) -> None:
    url_opened = False
    buf = RUNNING_PROCS[item_id]["logs"]
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.rstrip("\r\n")
            buf.append(clean)
            m = re.search(r"(https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+[^\s]*)", clean)
            if m:
                found_url = m.group(1).replace("0.0.0.0", "127.0.0.1")
                configured = RUNNING_PROCS[item_id].get("url")
                sub_path = RUNNING_PROCS[item_id].get("sub_path", "")
                if configured:
                    target_url = configured
                elif sub_path and not found_url.rstrip("/").endswith(sub_path.rstrip("/")):
                    target_url = found_url.rstrip("/") + sub_path
                else:
                    target_url = found_url
                RUNNING_PROCS[item_id]["url"] = target_url
                if not url_opened:
                    url_opened = True
                    try:
                        webbrowser.open(target_url)
                    except Exception:
                        pass
    except Exception as exc:
        buf.append(f"[stream error] {exc}")


class LaunchRequest(BaseModel):
    id: str
    command: Optional[str] = None
    cwd: Optional[str] = None
    url: Optional[str] = None
    launch_type: Optional[str] = None


class CommentRequest(BaseModel):
    id: str
    comment: str


class SkillToggleRequest(BaseModel):
    folder: str
    enable: bool


class ConfigureApiRequest(BaseModel):
    id: str
    values: Dict[str, str]
    scope: Optional[str] = "project"


class RootUpdateRequest(BaseModel):
    path: str
    action: str = "add"


@app.post("/api/roots")
def api_roots(req: RootUpdateRequest):
    cfg = load_config()
    roots = list(cfg.get("workspace_roots", ["auto"]))
    p = str(resolve_path(req.path)) if req.path.strip().lower() != "auto" else "auto"
    if req.action == "add":
        if p != "auto" and not Path(p).exists():
            raise HTTPException(status_code=404, detail=f"Folder not found: {p}")
        if p not in roots:
            roots.append(p)
    elif req.action == "remove":
        roots = [r for r in roots if r != req.path and r != p]
    cfg["workspace_roots"] = roots or ["auto"]
    save_config(cfg)
    return {"ok": True, "workspace_roots": cfg["workspace_roots"]}


@app.get("/")
def home():
    return FileResponse(UI_DIR / "index.html")


@app.get("/api/inventory")
def api_inventory():
    return build_inventory()


@app.post("/api/comment")
def api_comment(req: CommentRequest):
    data = load_comments()
    if req.comment.strip():
        data[req.id] = req.comment.strip()
    else:
        data.pop(req.id, None)
    save_comments(data)
    return {"ok": True, "id": req.id, "comment": data.get(req.id, "")}


@app.post("/api/launch")
def api_launch(req: LaunchRequest):
    inv = build_inventory()
    all_items = inv["repos"] + inv["skills"]
    match = next((x for x in all_items if x["id"] == req.id), None)
    cmd = req.command or (match["command"] if match else None)
    cwd = req.cwd or (match.get("cwd") or match.get("path") if match else str(APP_DIR))
    url = req.url or (match.get("url") if match else None)
    ltype = req.launch_type or (match.get("launch_type") if match else "web_server")

    if not cmd and not url:
        raise HTTPException(status_code=400, detail="No launch command or URL configured.")

    if ltype == "open_file" and url:
        if url.startswith("http"):
            webbrowser.open(url)
        elif os.name == "nt":
            os.startfile(url)
        else:
            webbrowser.open(Path(url).as_uri())
        return {"ok": True, "opened": url}

    with PROCESS_LOCK:
        existing = RUNNING_PROCS.get(req.id)
        if existing and existing["proc"].poll() is None:
            if existing.get("url"):
                webbrowser.open(existing["url"])
            return {"ok": True, "pid": existing["proc"].pid, "url": existing.get("url")}

        launch_env = _collect_launch_env(Path(match["path"]) if match else None, Path(cwd) if cwd else None)
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=launch_env,
        )
        RUNNING_PROCS[req.id] = {
            "proc": proc,
            "cmd": cmd,
            "url": url,
            "sub_path": match.get("sub_path", "") if match else "",
            "started_at": time.time(),
            "logs": deque(maxlen=250),
        }
        threading.Thread(target=_stream_proc_output, args=(req.id, proc), daemon=True).start()

    time.sleep(0.9)
    early_code = proc.poll()
    if early_code is not None and early_code != 0 and ltype == "web_server":
        with PROCESS_LOCK:
            logs = list(RUNNING_PROCS.get(req.id, {}).get("logs", []))
        return {"ok": False, "pid": proc.pid, "exit_code": early_code, "error": f"Process exited immediately (code {early_code})", "logs": logs}

    if url and early_code is None:
        threading.Timer(2.0, lambda: webbrowser.open(url) if proc.poll() is None else None).start()

    return {"ok": True, "pid": proc.pid, "url": url}


@app.post("/api/stop")
def api_stop(req: LaunchRequest):
    with PROCESS_LOCK:
        info = RUNNING_PROCS.get(req.id)
        if not info:
            return {"ok": True, "stopped": False}
        proc = info["proc"]
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.terminate()
        RUNNING_PROCS.pop(req.id, None)
    return {"ok": True, "stopped": True}


@app.get("/api/logs")
def api_logs(id: str):
    with PROCESS_LOCK:
        info = RUNNING_PROCS.get(id)
        if not info:
            return {"running": False, "logs": [], "url": None}
        return {
            "running": info["proc"].poll() is None,
            "exit_code": info["proc"].poll(),
            "url": info.get("url"),
            "logs": list(info["logs"]),
        }


@app.post("/api/configure-api")
def api_configure_api(req: ConfigureApiRequest):
    inv = build_inventory()
    match = next((x for x in (inv["repos"] + inv["stubs"] + inv["skills"]) if x["id"] == req.id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Item not found in inventory.")
    target_dir = APP_DIR if req.scope == "hub" else Path(match["path"])
    if not target_dir.exists():
        raise HTTPException(status_code=404, detail="Target folder not found.")
    env_file = target_dir / ".env"
    saved_keys = _upsert_env_file(env_file, req.values)
    if not saved_keys:
        raise HTTPException(status_code=400, detail="Provide at least one non-empty API key.")
    refreshed = detect_api_requirements(Path(match["path"]), Path(match.get("cwd") or match["path"]), is_skill=(match.get("kind") == "skill"))
    return {"ok": True, "saved_count": len(saved_keys), "env_file": str(env_file), "api": refreshed}


@app.post("/api/open-folder")
def api_open_folder(req: LaunchRequest):
    target = req.cwd
    if not target and req.id:
        inv = build_inventory()
        match = next((x for x in (inv["repos"] + inv["stubs"] + inv["skills"]) if x["id"] == req.id), None)
        if match:
            target = match.get("path") or match.get("cwd")
    if target and Path(target).exists():
        p = Path(target)
        folder = os.path.normpath(str(p if p.is_dir() else p.parent))
        if os.name == "nt":
            try:
                subprocess.Popen(
                    ["explorer.exe", folder],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            except Exception:
                os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return {"ok": True, "opened": folder}
    raise HTTPException(status_code=404, detail="Folder not found")


@app.post("/api/graphify-update")
def api_graphify_update():
    cfg = load_config()
    roots = cfg.get("workspace_roots", [".."])
    target = str(resolve_path(roots[0])) if roots else str(APP_DIR.parent)
    proc = subprocess.run(["graphify", "update", "."], cwd=target, capture_output=True, text=True, timeout=120)
    return {"ok": proc.returncode == 0, "output": (proc.stdout + "\n" + proc.stderr)[-2000:]}


@app.post("/api/skill-toggle")
def api_skill_toggle(req: SkillToggleRequest):
    cfg = load_config()
    skills_dir = resolve_path(cfg.get("skills_dir", "~/.gemini/config/skills"))
    master_raw = cfg.get("master_skills_dir", "")
    if not master_raw:
        raise HTTPException(status_code=400, detail="Set master_skills_dir in hub.config.json to toggle skill links.")
    src = resolve_path(master_raw) / req.folder
    dst = skills_dir / req.folder
    if not src.exists():
        raise HTTPException(status_code=404, detail="Skill folder not found in master_skills_dir")
    if req.enable and not dst.exists():
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], check=True)
        else:
            dst.symlink_to(src)
    elif not req.enable and dst.exists():
        if os.name == "nt":
            os.rmdir(dst)
        else:
            dst.unlink()
    return {"ok": True, "active": dst.exists()}


if UI_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")


if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run(app, host=cfg.get("host", "127.0.0.1"), port=int(cfg.get("port", 8787)))
