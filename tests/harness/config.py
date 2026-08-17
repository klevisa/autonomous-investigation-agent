"""Load tests/config/config.env and write discovered ids back to it.

Each phase loads a fresh `Config`
and `.set(key, value)` does the idempotent replace-or-append into the same file — so state (synthesized
profiles, discovered ids) flows from one phase to the next through the file the orchestrator's phases share.

The config file is chosen via $AIA_CONFIG (a relative name resolves under
tests/config/), else tests/config/config.env. So a multi-env run (staging in one workspace, prod in another)
points AIA_CONFIG at its own file and every write-back lands in the right place.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# repo layout: this file is tests/harness/config.py → parents[2] is the repo root.
HARNESS_DIR = Path(__file__).resolve().parent
TESTS_DIR = HARNESS_DIR.parent
REPO_ROOT = TESTS_DIR.parent


def _derive_gh_repo() -> str:
    """owner/repo from `git remote get-url origin` (https or ssh form), else "". Lets a cicd run work when
    GH_REPO is left blank in the config — the git remote already encodes owner/repo, so there's nothing to
    hand-fill and no silent 'can't run cicd' deep in a run."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"],
                             capture_output=True, text=True, cwd=str(REPO_ROOT)).stdout.strip()
    except Exception:  # noqa: BLE001 — no git / no origin → just don't derive
        return ""
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


def _config_path() -> Path:
    """Resolve the config file: $AIA_CONFIG (relative → tests/config/), else tests/config/config.env."""
    raw = os.environ.get("AIA_CONFIG", "")
    if not raw:
        return TESTS_DIR / "config" / "config.env"
    p = Path(raw)
    return p if p.is_absolute() else TESTS_DIR / "config" / raw


class Config:
    """The parsed config.env. Attribute-style reads via `.get`, and `.set` for the write-back.

    Values are the shell-unquoted RHS of `KEY="VALUE"` / `KEY=VALUE` lines (comments + blanks ignored).
    We keep the raw file text so `.set` can rewrite one line in place without reformatting the rest.
    """

    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            raise SystemExit(
                f"MISSING {path} — copy config.env.example and fill it in (or set AIA_CONFIG)."
            )
        self._text = path.read_text()
        self._vals = self._parse(self._text)
        # BUNDLE_TARGET drives every `-t` in the harness; default 'stage' keeps single-env runs working.
        self._vals.setdefault("BUNDLE_TARGET", "stage")
        # GH_REPO (needed only by the cicd path) auto-derives from the git origin when blank — one less value
        # to hand-fill. In-memory only (not written back); an explicit config value always wins.
        if not self._vals.get("GH_REPO"):
            derived = _derive_gh_repo()
            if derived:
                self._vals["GH_REPO"] = derived

    @staticmethod
    def _parse(text: str) -> dict[str, str]:
        vals: dict[str, str] = {}
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            key, _, rhs = s.partition("=")
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            # strip a trailing inline comment only when the value is unquoted (quoted values may contain #)
            rhs = rhs.strip()
            if rhs and rhs[0] in "\"'":
                q = rhs[0]
                end = rhs.find(q, 1)
                val = rhs[1:end] if end != -1 else rhs[1:]
            else:
                val = rhs.split("#", 1)[0].strip()
            # expand ${TEST_SUFFIX}-style refs to earlier vars (config.env uses these)
            val = re.sub(r"\$\{(\w+)\}", lambda m: vals.get(m.group(1), ""), val)
            vals[key] = val
        return vals

    def get(self, key: str, default: str = "") -> str:
        return self._vals.get(key, default)

    def require(self, key: str) -> str:
        v = self._vals.get(key, "")
        if not v:
            raise SystemExit(f"config.env: {key} is empty — fill it in (or an earlier step must write it back).")
        return v

    @property
    def bundle_target(self) -> str:
        return self._vals.get("BUNDLE_TARGET", "stage")

    @property
    def deploy_strategy(self) -> str:
        """'dev' | 'cicd' — how the deploy happens. Explicit DEPLOY_STRATEGY wins (legacy 'local' → 'dev');
        otherwise inferred from the target: prod deploys via CI, everything else locally."""
        s = self._vals.get("DEPLOY_STRATEGY", "").strip().lower()
        if s in ("dev", "local"):
            return "dev"
        if s == "cicd":
            return "cicd"
        return "cicd" if self.bundle_target == "prod" else "dev"

    def set(self, key: str, value: str) -> None:
        """Idempotently replace-or-append `key="value"` in the file, and update the in-memory value.

        If the key exists (even commented as `KEY=`), rewrite that line;
        otherwise append. Keeps the file readable for a human who inspects it after a run.
        """
        self._vals[key] = value
        line = f'{key}="{value}"'
        pat = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pat.search(self._text):
            self._text = pat.sub(line, self._text, count=1)
        else:
            if self._text and not self._text.endswith("\n"):
                self._text += "\n"
            self._text += line + "\n"
        self.path.write_text(self._text)


def load() -> Config:
    return Config(_config_path())
