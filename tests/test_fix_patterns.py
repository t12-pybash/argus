import pytest
import yaml

from argus.commands.fix_patterns import (
    match,
    render,
    _pod_to_deployment,
    _double_memory,
    _parse_memory_limit,
    _image_ref,
    _container_name,
    PATTERNS,
)


# ── _pod_to_deployment ────────────────────────────────────────────────────────

class TestPodToDeployment:
    def test_strips_replicaset_and_pod_hash(self):
        assert _pod_to_deployment("argus-9b4b79477-c574q") == "argus"

    def test_strips_long_replicaset_hash(self):
        assert _pod_to_deployment("external-dns-5b8cddcd5f-rxtqv") == "external-dns"

    def test_strips_grafana_style(self):
        assert _pod_to_deployment("kube-prometheus-stack-grafana-59895469d8-rwt72") == "kube-prometheus-stack-grafana"

    def test_plain_name_unchanged(self):
        assert _pod_to_deployment("my-app") == "my-app"

    def test_vault_ingest_consumer(self):
        assert _pod_to_deployment("vault-ingest-consumer-67fc46fdff-gg7v2") == "vault-ingest-consumer"


# ── _double_memory ────────────────────────────────────────────────────────────

class TestDoubleMemory:
    def test_doubles_mi(self):
        assert _double_memory("256Mi") == "512Mi"

    def test_doubles_gi(self):
        assert _double_memory("1Gi") == "2Gi"

    def test_mi_promotes_to_gi(self):
        assert _double_memory("512Mi") == "1Gi"

    def test_large_mi_promotes(self):
        assert _double_memory("2048Mi") == "4Gi"

    def test_doubles_ki(self):
        assert _double_memory("512Ki") == "1024Ki"

    def test_invalid_returns_default(self):
        assert _double_memory("garbage") == "1Gi"

    def test_empty_returns_default(self):
        assert _double_memory("") == "1Gi"

    def test_case_insensitive(self):
        assert _double_memory("512mi") == "1Gi"


# ── _parse_memory_limit ───────────────────────────────────────────────────────

DESCRIBE_WITH_LIMITS = """\
    Limits:
      memory:  512Mi
      cpu:     500m
    Requests:
      memory:  256Mi
"""

DESCRIBE_NO_LIMITS = """\
    Name:   my-container
    Image:  nginx:latest
"""

DESCRIBE_ONLY_MEMORY = "    memory: 1Gi"

class TestParseMemoryLimit:
    def test_extracts_from_limits_block(self):
        assert _parse_memory_limit(DESCRIBE_WITH_LIMITS) == "512Mi"

    def test_fallback_any_memory_line(self):
        assert _parse_memory_limit(DESCRIBE_ONLY_MEMORY) == "1Gi"

    def test_no_memory_returns_default(self):
        assert _parse_memory_limit(DESCRIBE_NO_LIMITS) == "512Mi"


# ── _image_ref ────────────────────────────────────────────────────────────────

class TestImageRef:
    def test_ghcr(self):
        describe = "  Image: ghcr.io/myorg/myapp:v1.2.3"
        assert _image_ref(describe) == ("ghcr.io", "ghcr-pull-secret")

    def test_gitlab(self):
        describe = "  Image: registry.gitlab.com/mygroup/myapp:latest"
        assert _image_ref(describe) == ("registry.gitlab.com", "gitlab-pull-secret")

    def test_dockerhub_explicit(self):
        describe = "  Image: docker.io/library/nginx:1.25"
        assert _image_ref(describe) == ("docker.io", "dockerhub-pull-secret")

    def test_dockerhub_no_registry(self):
        describe = "  Image: nginx:latest"
        assert _image_ref(describe) == ("docker.io", "dockerhub-pull-secret")

    def test_unknown_registry_defaults_to_ghcr(self):
        describe = "  Image: quay.io/prometheus/prometheus:v3.0"
        assert _image_ref(describe) == ("ghcr.io", "ghcr-pull-secret")

    def test_no_image_line_defaults_to_dockerhub(self):
        # empty image string: "docker.io" not in "" but "/" not in "" is True → dockerhub
        assert _image_ref("no image here") == ("docker.io", "dockerhub-pull-secret")


# ── _container_name ───────────────────────────────────────────────────────────

class TestContainerName:
    def test_extracts_container_name(self):
        describe = "  my-container:\n    Image: nginx\n"
        assert _container_name(describe, "fallback") == "my-container"

    def test_uses_fallback_when_not_found(self):
        assert _container_name("no containers here", "my-deploy") == "my-deploy"


# ── match ─────────────────────────────────────────────────────────────────────

class TestMatch:
    def test_matches_imagepullbackoff(self):
        p = match("ImagePullBackOff", "", "")
        assert p is not None
        assert p.name == "ImagePullBackOff"

    def test_matches_errimagepull(self):
        p = match("ErrImagePull", "", "")
        assert p is not None
        assert p.name == "ImagePullBackOff"

    def test_matches_oomkilled(self):
        p = match("OOMKilled", "", "")
        assert p is not None
        assert p.name == "OOMKilled"

    def test_matches_exit_137(self):
        p = match("", "exit 137", "")
        assert p is not None
        assert p.name == "OOMKilled"

    def test_matches_evicted(self):
        p = match("Evicted", "", "the node was low on resource: memory")
        assert p is not None
        assert p.name == "Evicted"

    def test_matches_crashloopbackoff(self):
        p = match("CrashLoopBackOff", "", "")
        assert p is not None
        assert p.name == "CrashLoopBackOff"
        assert p.use_llm is True

    def test_matches_missing_config_in_events(self):
        p = match("", "config file not found", "")
        assert p is not None
        assert p.name == "MissingConfigFile"

    def test_no_match_returns_none(self):
        assert match("Running", "normal events", "all good") is None

    def test_case_insensitive(self):
        assert match("IMAGEPULLBACKOFF", "", "") is not None


# ── render + YAML validity ────────────────────────────────────────────────────

DESCRIBE_OOM = """\
  my-app:
    Image:  ghcr.io/myorg/myapp:v1
    Limits:
      memory:  256Mi
      cpu:     200m
"""

DESCRIBE_PULL = """\
  my-app:
    Image:  ghcr.io/myorg/myapp:v1
"""

DESCRIBE_CONFIG = """\
  my-app:
    Image:  nginx:latest
"""

class TestRenderYaml:
    def _parse_all(self, yaml_str: str) -> list:
        return list(yaml.safe_load_all(yaml_str.lstrip("# \n").split("---", 1)[-1]))

    def _load_yaml_docs(self, out: str) -> list:
        clean = "---".join(d for d in out.split("---") if d.strip() and not d.strip().startswith("#"))
        return [d for d in yaml.safe_load_all(clean) if d]

    def test_oom_yaml_is_valid(self):
        p = next(x for x in PATTERNS if x.name == "OOMKilled")
        out = render(p, "prod", "my-app-9b4b79477-c574q", DESCRIBE_OOM)
        docs = self._load_yaml_docs(out)
        assert any(d.get("kind") == "Deployment" for d in docs)

    def test_oom_doubles_memory(self):
        p = next(x for x in PATTERNS if x.name == "OOMKilled")
        out = render(p, "prod", "my-app-9b4b79477-c574q", DESCRIBE_OOM)
        assert "512Mi" in out

    def test_imagepull_yaml_is_valid(self):
        p = next(x for x in PATTERNS if x.name == "ImagePullBackOff")
        out = render(p, "prod", "my-app-9b4b79477-c574q", DESCRIBE_PULL)
        docs = self._load_yaml_docs(out)
        assert any(d.get("kind") == "Deployment" for d in docs)

    def test_imagepull_contains_secret(self):
        p = next(x for x in PATTERNS if x.name == "ImagePullBackOff")
        out = render(p, "prod", "my-app-9b4b79477-c574q", DESCRIBE_PULL)
        assert "imagePullSecrets" in out
        assert "ghcr-pull-secret" in out

    def test_missing_config_yaml_is_valid(self):
        p = next(x for x in PATTERNS if x.name == "MissingConfigFile")
        out = render(p, "prod", "my-app-9b4b79477-c574q", DESCRIBE_CONFIG)
        docs = self._load_yaml_docs(out)
        kinds = {d.get("kind") for d in docs}
        assert "ConfigMap" in kinds
        assert "Deployment" in kinds

    def test_evicted_yaml_is_valid(self):
        p = next(x for x in PATTERNS if x.name == "Evicted")
        out = render(p, "prod", "my-app-9b4b79477-c574q", "")
        docs = self._load_yaml_docs(out)
        assert any(d.get("kind") == "Deployment" for d in docs)

    def test_crashloop_render_returns_empty(self):
        p = next(x for x in PATTERNS if x.name == "CrashLoopBackOff")
        assert render(p, "prod", "my-app-9b4b79477-c574q", "") == ""

    def test_namespace_in_output(self):
        p = next(x for x in PATTERNS if x.name == "OOMKilled")
        out = render(p, "my-namespace", "my-app-9b4b79477-c574q", DESCRIBE_OOM)
        assert "my-namespace" in out

    def test_deployment_name_stripped_of_hash(self):
        p = next(x for x in PATTERNS if x.name == "OOMKilled")
        out = render(p, "prod", "my-app-9b4b79477-c574q", DESCRIBE_OOM)
        assert "my-app-9b4b79477-c574q" not in out
        assert "name: my-app" in out
