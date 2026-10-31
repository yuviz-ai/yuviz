"""Settings live in one place: the root .env (template: .env.example).

1. Every environment variable the Python services read is listed in
   .env.example, so nothing configurable is hidden in code.
2. .env.example carries no real secret values.
3. No committed non-test file holds a credential: FreeSWITCH's public ESL
   default password, or a URL with an embedded password.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from services.config import platform_ai as _platform_ai

_REPO = Path(__file__).resolve().parents[1]
_EXAMPLE = _REPO / ".env.example"

# Helpers that read os.environ by name (pipeline_config._env, email._env, ...).
_ENV_HELPERS = {"_env", "_env_int", "_env_float", "_env_bool"}
_SECRET_NAME = re.compile(r"(PASSWORD|SECRET|_KEY|TOKEN)$")


def _connector_keys(auth_kind: str) -> list[str]:
    """OAuthProvider(key=...) entries in oauth.py's PROVIDERS for one auth_kind (absent = "oauth2"),
    read from source so a new connector's env vars can't ship undocumented."""
    tree = ast.parse((_REPO / "services/toolexec/oauth.py").read_text())
    keys = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "OAuthProvider":
            kw = {k.arg: k.value.value for k in node.keywords if isinstance(k.value, ast.Constant)}
            if kw.get("auth_kind", "oauth2") == auth_kind:
                keys.append(kw["key"].upper())
    assert keys, f"no {auth_kind} connectors found in oauth.py — the scan is broken"
    return keys


# Env reads whose name is computed, keyed (file, name expression) -> the names it can produce.
_OAUTH2_PROVIDERS = _connector_keys("oauth2")
_API_KEY_PROVIDERS = _connector_keys("api_key")
_DYNAMIC_READS = {
    ("services/conversation/__main__.py", "f'VOICEAI_ENABLE_{leg}'"): {"VOICEAI_ENABLE_STT", "VOICEAI_ENABLE_TTS"},
    # Bodies of the _env helpers; their callers are scanned by name.
    ("services/config/email.py", "name"): set(),
    ("services/conversation/pipeline_config.py", "key"): set(),
    ("services/conversation/language.py", "name"): set(),
    ("services/conversation/providers/http_pool.py", "name"): set(),
    # env:NAME secret refs: the name comes from a provider config, not code.
    ("libs/config_sdk/secret_resolver.py", "key"): set(),
    # Platform-managed AI: each catalogue option names the root .env key it runs on.
    ("services/config/platform_ai.py", "option.key_env or ''"): {o.key_env for o in _platform_ai.OPTIONS.values() if o.key_env},
    # Bodies of the knowledge service's env helpers (envparse.env_int/env_float/..., limits/resilience config readers) and
    # the parse child's thread-pool loop; their callers pass literal names and are listed in docs/knowledge-env-vars.md.
    ("services/knowledge/chunker.py", "name"): set(),
    ("services/knowledge/envparse.py", "name"): set(),
    ("services/knowledge/parsers/_worker.py", "name"): set(),
    ("services/knowledge/parsers/limits.py", "name"): set(),
    ("services/knowledge/resilience.py", "name"): set(),
    # services/toolexec/oauth.py: per-provider connector config, keyed by OAuthProvider.key.
    ("services/toolexec/oauth.py", "_REDIRECT_URI_ENV"): {"TOOLEXEC_OAUTH_REDIRECT_URI"},
    ("services/toolexec/oauth.py", "f'TOOLEXEC_OAUTH_{provider.key.upper()}_CLIENT_ID'"):
        {f"TOOLEXEC_OAUTH_{p}_CLIENT_ID" for p in _OAUTH2_PROVIDERS},
    ("services/toolexec/oauth.py", "f'TOOLEXEC_OAUTH_{provider.key.upper()}_CLIENT_SECRET_REF'"):
        {f"TOOLEXEC_OAUTH_{p}_CLIENT_SECRET_REF" for p in _OAUTH2_PROVIDERS},
    ("services/toolexec/oauth.py", "f'TOOLEXEC_OAUTH_{provider.key.upper()}_REQUIRED_SCOPES'"):
        {f"TOOLEXEC_OAUTH_{p}_REQUIRED_SCOPES" for p in _OAUTH2_PROVIDERS},
    ("services/toolexec/oauth.py", "f'TOOLEXEC_{provider.key.upper()}_ENABLED'"):
        {f"TOOLEXEC_{p}_ENABLED" for p in _API_KEY_PROVIDERS},
}


def _tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", *patterns], cwd=_REPO, capture_output=True, text=True, check=True)
    return [_REPO / p for p in out.stdout.split()]


def _is_test(path: Path) -> bool:
    rel = path.relative_to(_REPO).parts
    return "tests" in rel or rel[0] == "tests" or path.name.endswith("_test.cpp")


# The knowledge/RAG service has ~130 tuning knobs (deadlines, caps, limits) whose safe default is "unset". Listing them all
# in .env.example would mean blank values, and a blank is not "unset" for every reader (an empty provider allow-list means
# "allow nothing"), so they are catalogued in docs/knowledge-env-vars.md instead: generated from the code by
# scripts/gen_knowledge_env_doc.py and drift-gated by services/knowledge/tests/test_env_doc.py. A variable must be in
# .env.example or in that document; the operator-facing switches (KNOWLEDGE_BINARY_PARSERS) are in both.
_KNOWLEDGE_DOC = _REPO / "docs" / "knowledge-env-vars.md"


def _knowledge_doc_names() -> set[str]:
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", _KNOWLEDGE_DOC.read_text(), flags=re.M)) if _KNOWLEDGE_DOC.exists() else set()


def _catalogue() -> dict[str, str]:
    pairs = (line.split("=", 1) for line in _EXAMPLE.read_text().splitlines() if re.match(r"^[A-Z][A-Z0-9_]*=", line))
    return {k: v for k, v in pairs}


def _env_names_read_by(path: Path) -> tuple[set[str], set[str]]:
    """(names read, unresolved name expressions)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    constants = {
        t.id: n.value.value
        for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        for t in n.targets if isinstance(t, ast.Name)
    }
    names, unresolved = set(), set()
    rel = path.relative_to(_REPO).as_posix()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args:
            fn = ast.unparse(n.func)
            if fn.endswith(("environ.get", "getenv", "environ.setdefault")) or fn in _ENV_HELPERS:
                arg = n.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
                elif isinstance(arg, ast.Name) and arg.id in constants:
                    names.add(constants[arg.id])
                else:
                    unresolved.add(f"{rel}::{ast.unparse(arg)}")
        if isinstance(n, ast.Subscript) and ast.unparse(n.value).endswith("environ"):
            if isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                names.add(n.slice.value)
            else:
                unresolved.add(f"{rel}::{ast.unparse(n.slice)}")
    return {x for x in names if re.fullmatch(r"[A-Z][A-Z0-9_]+", x)}, unresolved


def test_every_env_var_the_services_read_is_in_env_example():
    sources = [p for p in _tracked("services/*.py", "libs/*.py") if not _is_test(p) and "generated" not in p.parts]
    read, unresolved = set(), set()
    for p in sources:
        names, dynamic = _env_names_read_by(p)
        read |= names
        unresolved |= dynamic
    assert len(read) > 30, "the scan found almost nothing; it is not looking at the services"
    known = {f"{f}::{expr}" for f, expr in _DYNAMIC_READS}
    assert not unresolved - known, (
        f"env reads with a computed name; add them to _DYNAMIC_READS with the names they produce: {sorted(unresolved - known)}"
    )
    assert known <= unresolved, f"stale _DYNAMIC_READS entries: {sorted(known - unresolved)}"
    read |= {name for names in _DYNAMIC_READS.values() for name in names}
    missing = read - set(_catalogue()) - _knowledge_doc_names()
    assert not missing, f"read by code but not listed in .env.example or docs/knowledge-env-vars.md: {sorted(missing)}"


def test_env_example_has_no_secret_values():
    filled = [k for k, v in _catalogue().items() if _SECRET_NAME.search(k) and not k.endswith("_REF") and v.strip()]
    assert not filled, f".env.example must leave secrets blank: {filled}"


_CREDENTIALED_URL = re.compile(r"[a-z][a-z0-9+]*://[^\s/:@\"'`]+:([^\s@\"'`]+)@")


def _is_placeholder(secret: str) -> bool:
    return secret.startswith("<") or "$" in secret or secret in {"pass", "password"}


def test_no_committed_file_holds_a_credential():
    files = [p for p in _tracked() if not _is_test(p) and p.is_file() and p.suffix not in {".png", ".jpg", ".onnx", ".xlsx", ".csv"}]
    leaks = []
    for p in files:
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = p.relative_to(_REPO)
        if "ClueCon" in text:
            leaks.append(f"{rel}: FreeSWITCH's default ESL password")
        for m in _CREDENTIALED_URL.finditer(text):
            if not _is_placeholder(m.group(1)):
                leaks.append(f"{rel}: URL with an embedded password ({m.group(0)[:40]}…)")
    assert not leaks, "credentials belong in .env, not the repository:\n  " + "\n  ".join(leaks)
