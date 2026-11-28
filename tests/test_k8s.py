"""The Kubernetes manifests (deploy/k8s/): what must hold without a cluster. CI's `k8s` job deploys them on kind and
runs the smoke test (scripts/k8s.py smoke) for what only a cluster can show."""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from atlas_realms.regions import db_name, load

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "k8s"
OVERLAY = K8S / "overlays" / "dev"
sys.path.insert(0, str(ROOT / "scripts"))
import k8s  # noqa: E402


def docs() -> list[dict]:
    out = []
    for f in sorted(K8S.rglob("*.yaml")):
        out += [d for d in yaml.safe_load_all(f.read_text()) if d and d.get("kind") != "Kustomization"]
    return out


def of(kind: str) -> list[dict]:
    return [d for d in docs() if d["kind"] == kind]


def pod_specs() -> list[tuple[str, dict]]:
    out = []
    for d in docs():
        if d["kind"] in ("Deployment", "Job"):
            out.append((d["metadata"]["name"], d["spec"]["template"]))
    return out


def test_the_committed_regions_manifest_is_current():
    """regions.yaml is generated from regions.txt (make k8s-regions); a hand edit or a forgotten regeneration fails."""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "k8s.py"), "regions", "--check"], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


def test_every_region_is_an_arena_region_with_its_own_database_and_one_worker():
    regions = k8s.read_regions()
    assert len(regions) >= 2 and set(regions) <= set(load())
    dbs = {d["metadata"]["labels"]["realms.dragonhound.dev/region"]: d["spec"] for d in of("Database")}
    assert set(dbs) == set(regions)
    for r, spec in dbs.items():
        assert spec["name"] == db_name(r) and spec["cluster"]["name"] == "realms-db"
        assert [e["name"] for e in spec["extensions"]] == list(k8s.EXTENSIONS)
        assert spec["databaseReclaimPolicy"] == "retain"  # deleting the resource never drops a world
    workers = [d for d in of("Deployment") if d["metadata"]["labels"]["app.kubernetes.io/name"] == "realms-tick"]
    assert sorted(w["metadata"]["labels"]["realms.dragonhound.dev/region"] for w in workers) == sorted(regions)
    for w in workers:
        region = w["metadata"]["labels"]["realms.dragonhound.dev/region"]
        assert w["spec"]["replicas"] == 1 and w["spec"]["strategy"]["type"] == "Recreate"  # never two at once
        cmd = w["spec"]["template"]["spec"]["containers"][0]["command"]
        assert cmd[cmd.index("--region") + 1] == region and "--loop" in cmd
    assert of("ConfigMap")[0]["data"]["regions"] == ",".join(regions)


def test_render_refuses_a_region_the_arena_doesnt_have():
    with pytest.raises(ValueError, match="not in arena/regions.json"):
        k8s.render(["cook-strait", "atlantis"])


SECRETISH = re.compile(r"pass(word)?|secret|token|credential", re.I)


def secret_leaks(node, where: str = "") -> list[str]:
    """Literal credentials anywhere in a manifest: an env var named like a password with a `value` (only `valueFrom`
    with a `secretKeyRef` is allowed), or any mapping key named like one holding a literal (ConfigMap data and the like).
    """
    out = []
    if isinstance(node, dict):
        name = node.get("name")
        literal = "value" in node or ("valueFrom" in node and "secretKeyRef" not in node["valueFrom"])
        if isinstance(name, str) and SECRETISH.search(name) and literal:
            out.append(f"{where}: {name} is not from a secretKeyRef")
        for k, v in node.items():
            if (
                isinstance(k, str)
                and SECRETISH.search(k)
                and isinstance(v, (str, int, float))
                and not isinstance(v, bool)
            ):
                out.append(f"{where}.{k}: a literal")
            out += secret_leaks(v, f"{where}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += secret_leaks(v, f"{where}[{i}]")
    return out


def test_no_secret_is_committed():
    """The database owner's password is created out of band (scripts/k8s.py deploy, docs/deploy.md)."""
    assert of("Secret") == []
    manifests = docs()
    assert "PGPASSWORD" in "\n".join(f.read_text() for f in K8S.rglob("*.yaml"))  # the env var exists, from a Secret
    assert [leak for d in manifests for leak in secret_leaks(d, d.get("metadata", {}).get("name", d["kind"]))] == []


@pytest.mark.parametrize(
    "doc",
    [
        {"env": [{"name": "PGPASSWORD", "value": "hunter2"}]},
        {"env": [{"name": "POSTGRES_PASSWORD", "valueFrom": {"configMapKeyRef": {"name": "c", "key": "p"}}}]},
        {"kind": "ConfigMap", "data": {"PGPASSWORD": "hunter2"}},
        {"kind": "ConfigMap", "data": {"db_password": "hunter2"}},
    ],
)
def test_the_secret_check_catches_a_literal_credential(doc):
    assert secret_leaks(doc)


def test_every_pod_is_unprivileged_bounded_and_pinned_at_deploy():
    for name, tmpl in pod_specs():
        spec = tmpl["spec"]
        assert spec["automountServiceAccountToken"] is False, name
        assert spec["securityContext"]["runAsNonRoot"] is True, name
        for c in spec.get("initContainers", []) + spec["containers"]:
            # A placeholder, replaced by the image-id tag `scripts/k8s.py pin` writes: never a floating tag.
            assert c["image"] == "atlas-realms:unpinned", (name, c["image"])
            assert c["imagePullPolicy"] == "IfNotPresent"
            assert {"cpu", "memory"} <= set(c["resources"]["requests"]), name
            assert "memory" in c["resources"]["limits"], name
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True, name
            assert sc["capabilities"]["drop"] == ["ALL"], name
    assert of("Cluster")[0]["spec"]["imageName"] == "atlas-realms-db:unpinned"
    assert "latest" not in "\n".join(f.read_text() for f in K8S.rglob("*.yaml"))


def test_the_pinned_overlay_replaces_both_placeholders(tmp_path):
    text = k8s.pinned_kustomization(OVERLAY, k8s.PINNED, "atlas-realms:0123456789ab", "atlas-realms-db:ba9876543210")
    pinned = yaml.safe_load(text)
    assert pinned["resources"] == ["../../deploy/k8s/overlays/dev"]
    assert pinned["images"] == [{"name": "atlas-realms", "newTag": "0123456789ab"}]
    assert "atlas-realms-db:ba9876543210" in pinned["patches"][0]["patch"]


def test_only_db_clients_reach_postgres_and_nothing_reaches_the_workers():
    policies = {p["metadata"]["name"]: p["spec"] for p in of("NetworkPolicy")}
    deny = policies["default-deny-ingress"]
    assert deny["podSelector"] == {} and deny["policyTypes"] == ["Ingress"] and "ingress" not in deny
    db = policies["realms-db-ingress"]
    assert db["podSelector"]["matchLabels"] == {"cnpg.io/cluster": "realms-db"}
    sources = [peer for rule in db["ingress"] for peer in rule["from"]]
    assert {"realms.dragonhound.dev/db-client": "true"} in [
        p.get("podSelector", {}).get("matchLabels") for p in sources
    ]
    assert all("ipBlock" not in p for p in sources)
    # Exactly the API, the workers and the migrate job carry the db-client label.
    clients = sorted(
        name
        for name, tmpl in pod_specs()
        if tmpl["metadata"]["labels"].get("realms.dragonhound.dev/db-client") == "true"
    )
    assert clients == sorted(["realms-api", "realms-migrate", *(f"realms-tick-{r}" for r in k8s.read_regions())])
    # No ingress rule and no Service selects a worker.
    for p in policies.values():
        if "Ingress" in p["policyTypes"] and p.get("ingress"):
            assert p["podSelector"].get("matchLabels", {}).get("app.kubernetes.io/name") != "realms-tick"
    for s in of("Service"):
        assert s["spec"]["selector"].get("app.kubernetes.io/name") != "realms-tick"


def test_the_api_is_ready_only_on_health_and_alive_without_it():
    """/health is 503 until the regions are migrated and seeded: right for readiness, fatal as a liveness probe."""
    api = next(d for d in of("Deployment") if d["metadata"]["name"] == "realms-api")
    c = api["spec"]["template"]["spec"]["containers"][0]
    assert c["readinessProbe"]["httpGet"]["path"] == "/health"
    assert "httpGet" not in c["livenessProbe"]
    hpa = of("HorizontalPodAutoscaler")[0]["spec"]
    assert hpa["scaleTargetRef"]["name"] == "realms-api" and hpa["maxReplicas"] > 1


def test_the_db_image_tag_starts_with_its_postgres_version():
    """CloudNativePG reads the server version from the tag and refuses one that doesn't start with it."""
    assert k8s.postgres_version() == "16.16"


def test_the_operator_is_pinned_by_version_and_digest():
    assert k8s.CNPG_VERSION in k8s.CNPG_URL and len(k8s.CNPG_SHA256) == 64


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("atlas-realms:0123456789ab", ("newTag", "0123456789ab")),
        ("localhost:5000/atlas-realms:0123456789ab", ("newTag", "0123456789ab")),
        ("registry.example:5000/team/atlas-realms@sha256:" + "a" * 64, ("digest", "sha256:" + "a" * 64)),
    ],
)
def test_the_image_tag_is_after_the_last_colon_past_the_registry(ref, expected):
    assert k8s.image_ref(ref) == expected
    pinned = yaml.safe_load(k8s.pinned_kustomization(OVERLAY, k8s.PINNED, ref, "atlas-realms-db:x"))
    assert pinned["images"] == [{"name": "atlas-realms", expected[0]: expected[1]}]


@pytest.mark.parametrize("ref", ["atlas-realms", "localhost:5000/atlas-realms"])
def test_an_image_without_a_tag_is_refused(ref):
    with pytest.raises(SystemExit, match="no tag"):
        k8s.image_ref(ref)


@pytest.mark.parametrize(("body", "expected"), [(b"<html>502 Bad Gateway</html>", "<html>502"), (b"", "")])
def test_a_non_json_error_body_still_returns_the_status(monkeypatch, body, expected):
    import io
    import urllib.error

    def refuse(url, timeout):
        raise urllib.error.HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(body))

    monkeypatch.setattr(k8s.urllib.request, "urlopen", refuse)
    status, data = k8s.http_json("http://127.0.0.1:1/health")
    assert status == 502 and data["raw"].startswith(expected)


def test_a_failed_migration_fails_at_once_with_its_logs(monkeypatch):
    calls = []

    def fake(*args, **kw):
        calls.append(args)
        if "logs" in args:
            return "relation already exists\n"
        return '[{"type":"Failed","status":"True","reason":"BackoffLimitExceeded"}]'

    monkeypatch.setattr(k8s, "kubectl", fake)
    monkeypatch.setattr(k8s.time, "sleep", lambda s: pytest.fail("polled again after the job had failed"))
    with pytest.raises(SystemExit, match="(?s)BackoffLimitExceeded.*relation already exists"):
        k8s.wait_job("realms-migrate", 900)
    monkeypatch.setattr(k8s, "kubectl", lambda *a, **kw: '[{"type":"Complete","status":"True"}]')
    k8s.wait_job("realms-migrate", 900)


def test_a_probe_that_times_out_fails_cleanly_and_removes_its_pod(monkeypatch):
    deleted = []

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(k8s.subprocess, "run", slow)
    monkeypatch.setattr(k8s, "kubectl", lambda *a, **kw: deleted.append(a) or "")
    first = k8s.reach("np-probe-stranger", False, "atlas-realms:x")
    assert first.startswith("timed out")  # never "refused": a timeout is not a verdict
    k8s.reach("np-probe-stranger", False, "atlas-realms:x")
    pods = [a[a.index("pod") + 1] for a in deleted]
    assert all(a[:4] == ("-n", k8s.NAMESPACE, "delete", "pod") for a in deleted)
    assert len(pods) == 2 and pods[0] != pods[1]  # unique names: a leftover pod can't break the next run


def test_the_smoke_verdicts_survive_python_O(monkeypatch):
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(k8s))
    assert not [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
    with pytest.raises(SystemExit, match="FAILED: ticks lost"):
        k8s.check(False, "ticks lost")


def test_no_worker_pod_is_a_clear_failure(monkeypatch):
    monkeypatch.setattr(k8s, "kubectl", lambda *a, **kw: "")
    with pytest.raises(SystemExit, match="no tick worker pod for region cook-strait"):
        k8s.worker_pods("cook-strait")
