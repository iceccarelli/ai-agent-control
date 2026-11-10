"""The carry-research image and Fly config: public-data research only. Static checks (the real container checks run in CI: .github/workflows/research-image.yml)."""
import ast
import os
import re
import sys

BOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BOT)
TOOLS = os.path.join(BOT, "tools")


def read(*p):
    return open(os.path.join(*p), encoding="utf-8").read()


def local_closure(start):
    seen, todo = set(), [start]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = os.path.join(TOOLS, mod + ".py")
        for n in ast.walk(ast.parse(read(path))):
            names = [a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import) else ([n.module.split(".")[0]] if isinstance(n, ast.ImportFrom) and n.module else [])
            for nm in names:
                if os.path.exists(os.path.join(TOOLS, nm + ".py")) or os.path.exists(os.path.join(BOT, nm + ".py")):
                    todo.append(nm)
    return seen


def copied():
    d = read(BOT, "Dockerfile.research")
    out = set()
    for m in re.finditer(r"^COPY (tools/[^\n]*?) \./tools/\s*$", d, re.M):
        out |= {x[len("tools/"):-3] for x in m.group(1).split()}
    return out


def test_the_image_ships_exactly_the_import_closure_of_the_worker_and_nothing_that_can_reach_a_venue():
    closure = local_closure("research_service")
    assert copied() == closure, (sorted(copied() ^ closure))
    assert not [m for m in closure if re.search(r"bybit|venue|ms_|exec_|supabase|transport|market_", m)], closure
    assert all(os.path.exists(os.path.join(TOOLS, m + ".py")) for m in closure)


def test_no_credential_name_venue_switch_or_blanket_copy_in_the_image_files():
    for name in ("Dockerfile.research", "fly.research.toml", os.path.join("deploy", "research-entrypoint.sh")):
        text = read(BOT, name)
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for bad in ("BYBIT", "SUPABASE", "API_KEY", "API_SECRET", "LIVE_TRADING", "PAPER_TRADING", "USE_TESTNET", "TOKEN", "PASSWORD", "pip install", "requirements"):
            assert bad.lower() not in code.lower(), (name, bad)
        assert not re.search(r"^COPY (\.|tools/?|\*) ", code, re.M), name
        assert "ARG " not in code


def test_the_fly_config_has_no_service_no_env_no_secret_and_one_volume_machine():
    t = read(BOT, "fly.research.toml")
    code = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    assert 'app = "carry-research"' in code and 'primary_region = "fra"' in code and 'dockerfile = "Dockerfile.research"' in code
    for forbidden in ("[http_service]", "[[services]]", "[env]", "[[statics]]", "[processes]", "secrets"):
        assert forbidden not in code
    assert re.search(r'\[\[restart\]\]\npolicy = "always"', code) and re.search(r'\[\[mounts\]\]\nsource = "research_data"\ndestination = "/data"', code)
    assert code.count("[[vm]]") == 1 and code.count("[[mounts]]") == 1 and 'kill_signal = "SIGTERM"' in code


def test_the_entrypoint_drops_privileges_and_execs_and_the_image_runs_the_research_worker():
    e = read(BOT, "deploy", "research-entrypoint.sh")
    assert "set -eu" in e and re.search(r"^exec setpriv --reuid=bot --regid=bot --init-groups \"\$@\"$", e, re.M) and "chmod 700 /data/research" in e
    d = read(BOT, "Dockerfile.research")
    assert 'ENTRYPOINT ["/usr/local/bin/research-entrypoint"]' in d and "tools/research_service.py" in d.split("CMD", 1)[1] and "HOME=/data" in d
    assert os.access(os.path.join(BOT, "deploy", "research-entrypoint.sh"), os.X_OK) and os.access(os.path.join(BOT, "deploy", "research-image-smoke.sh"), os.X_OK)


def test_the_ci_workflow_builds_and_runs_the_real_container_with_no_secret():
    w = read(REPO, ".github", "workflows", "research-image.yml")
    assert re.search(r"^permissions:\n  contents: read\n", w, re.M) and "secrets." not in w and "deploy/research-image-smoke.sh" in w
    for ref in re.findall(r"^\s*-?\s*uses:\s*(\S+)", w, re.M):
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref
    s = read(BOT, "deploy", "research-image-smoke.sh")
    for needed in ("docker build -f Dockerfile.research", "docker stop -t 20", "!= \"137\"", "agent_contracts.py dataset_inventory.py research_jobs.py research_service.py", "exchange_credentials"):
        assert needed in s, needed
