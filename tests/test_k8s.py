"""The Kubernetes manifests (deploy/k8s/): what must hold without a cluster. CI's `k8s` job deploys them on kind and
runs the smoke test (scripts/k8s.py smoke) for what only a cluster can show."""

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


def test_no_secret_is_committed():
    """The database owner's password is created out of band (scripts/k8s.py deploy, docs/deploy.md)."""
    assert of("Secret") == []
    text = "\n".join(f.read_text() for f in K8S.rglob("*.yaml"))
    assert "PGPASSWORD" in text and "password:" not in text.replace("key: password", "")


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
