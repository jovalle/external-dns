"""Integration tests for external-dns using a full Docker Compose stack.

=============================================================================
Test Stack Overview
=============================================================================

The integration test stack (docker-compose.yaml) consists of:

1. **traefik** - Reverse proxy with HTTP API enabled on port 8080
   - Routes configured via Docker labels on containers
   - Exposes API for external-dns to discover routers

2. **adguard** - AdGuard Home DNS server with web API on port 3000
   - DNS rewrites managed via /control/rewrite/* endpoints
   - Credentials: admin/password (from example config)

3. **whoami** - Test service container with Traefik labels
   - Has label: traefik.http.routers.whoami-internal.rule: Host(`whoami-internal.localtest.me`)
   - This creates a router that external-dns should discover and sync

4. **external-dns** - The application under test
   - Polls Traefik API for routers
   - Syncs discovered routes to AdGuard DNS rewrites

=============================================================================
Route Configuration
=============================================================================

Routes are configured via Docker labels on containers:

    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.{name}.rule=Host(`{hostname}`)"

The test container (whoami) uses:
    - Router name: whoami-internal (ends with -internal for zone detection)
    - Hostname: whoami-internal.localtest.me

=============================================================================
Expected Sync Behavior
=============================================================================

1. **Route Added -> DNS Record Appears**
   - When Traefik sees a new router, external-dns discovers it on next poll
   - A DNS rewrite is created in AdGuard: hostname -> target_ip

2. **Route Removed -> DNS Record Deleted**
   - When a Traefik router is removed, external-dns detects absence
   - The corresponding DNS rewrite is deleted from AdGuard

3. **Idempotent Sync**
   - Running sync multiple times produces the same DNS state
   - No duplicate records are created

=============================================================================
Test Prerequisites
=============================================================================

- Docker must be installed and running
- Set EXTERNAL_DNS_RUN_DOCKER_TESTS=1 to enable these tests
- Tests are slow (~60-90 seconds) due to Docker operations

=============================================================================
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml


def _run(
    cmd: list[str], env: dict[str, str] | None = None, timeout: int = 300
) -> subprocess.CompletedProcess:
    """Execute a shell command and capture output.

    Args:
        cmd: Command and arguments as a list
        env: Environment variables (optional, inherits from parent if None)
        timeout: Maximum execution time in seconds

    Returns:
        CompletedProcess with stdout/stderr captured as text
    """
    return subprocess.run(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def _step(message: str) -> None:
    """Print a formatted progress message for test output.

    Args:
        message: Progress message to display
    """
    print(f"[integration] {message}", flush=True)


def _wait_for_container_health(
    container_name: str,
    *,
    timeout_seconds: int = 60,
    poll_seconds: float = 2.0,
) -> str:
    """Wait for Docker container health to reach a terminal state.

    Polls Docker inspect to check the container's health status. Returns when
    the container reports "healthy", "unhealthy", or "none" (no healthcheck),
    or when the timeout is reached.

    Args:
        container_name: Name of the Docker container to check
        timeout_seconds: Maximum time to wait for health status
        poll_seconds: Interval between health checks

    Returns:
        Final health status string: "healthy", "unhealthy", "none", "missing", or "starting"
    """
    deadline = time.time() + timeout_seconds
    last_status = "unknown"
    while time.time() < deadline:
        out = _run(
            [
                "docker",
                "inspect",
                "-f",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                container_name,
            ]
        )
        if out.returncode != 0:
            last_status = "missing"
            _step(f"Health check: {container_name} not inspectable yet")
            time.sleep(poll_seconds)
            continue

        last_status = out.stdout.strip()
        _step(f"Health check: {container_name} -> {last_status}")
        if last_status in ("healthy", "unhealthy", "none"):
            return last_status

        time.sleep(poll_seconds)

    return last_status


def _docker_available() -> bool:
    """Check if Docker CLI is available on the system.

    Returns:
        True if 'docker' command is found in PATH
    """
    return shutil.which("docker") is not None


# =============================================================================
# Test Helper Functions
# =============================================================================


def _get_adguard_rewrites(dc_func) -> list[dict[str, Any]]:
    """Fetch current DNS rewrites from AdGuard API.

    Uses the external-dns container to make the API request (to stay within
    the Docker network).

    Args:
        dc_func: Function to run docker compose commands

    Returns:
        List of rewrite dicts with 'domain' and 'answer' keys, or empty list on error
    """
    cmd = dc_func(
        "exec",
        "-T",
        "external-dns",
        "python3",
        "-c",
        (
            "import json,requests; "
            "r=requests.get('http://adguard:3000/control/rewrite/list',auth=('admin','password'),timeout=5); "
            "r.raise_for_status(); "
            "print(json.dumps(r.json()))"
        ),
    )
    if cmd.returncode != 0:
        return []
    try:
        return json.loads(cmd.stdout.strip())
    except (json.JSONDecodeError, AttributeError):
        return []


def _get_traefik_routers(dc_func) -> list[dict[str, Any]]:
    """Fetch current HTTP routers from Traefik API.

    Uses the external-dns container to make the API request (to stay within
    the Docker network).

    Args:
        dc_func: Function to run docker compose commands

    Returns:
        List of router dicts from Traefik API, or empty list on error
    """
    cmd = dc_func(
        "exec",
        "-T",
        "external-dns",
        "python3",
        "-c",
        (
            "import json,requests; "
            "r=requests.get('http://traefik:8080/api/http/routers',timeout=5); "
            "r.raise_for_status(); "
            "print(json.dumps(r.json()))"
        ),
    )
    if cmd.returncode != 0:
        return []
    try:
        return json.loads(cmd.stdout.strip())
    except (json.JSONDecodeError, AttributeError):
        return []


def _assert_rewrite_exists(
    rewrites: list[dict[str, Any]], domain: str, expected_answer: str | None = None
) -> None:
    """Assert that a DNS rewrite exists for the given domain.

    Args:
        rewrites: List of rewrite dicts from AdGuard API
        domain: Domain name to look for
        expected_answer: If provided, also verify the answer matches

    Raises:
        AssertionError: If rewrite not found or answer doesn't match
    """
    for entry in rewrites:
        if entry.get("domain") == domain:
            if expected_answer is not None:
                actual = str(entry.get("answer", "")).strip()
                assert (
                    actual == expected_answer
                ), f"Rewrite for '{domain}' has wrong answer: expected={expected_answer}, got={actual}"
            return
    raise AssertionError(f"Rewrite not found for domain '{domain}'. Rewrites: {rewrites}")


def _assert_rewrite_not_exists(rewrites: list[dict[str, Any]], domain: str) -> None:
    """Assert that NO DNS rewrite exists for the given domain.

    Args:
        rewrites: List of rewrite dicts from AdGuard API
        domain: Domain name that should NOT exist

    Raises:
        AssertionError: If rewrite is found for the domain
    """
    for entry in rewrites:
        if entry.get("domain") == domain:
            raise AssertionError(
                f"Rewrite should not exist for domain '{domain}', but found: {entry}"
            )


def _wait_for_rewrite(
    dc_func,
    domain: str,
    expected_answer: str | None = None,
    timeout_seconds: int = 90,
    poll_seconds: float = 2.0,
) -> dict[str, Any] | None:
    """Wait for a DNS rewrite to appear in AdGuard.

    Polls the AdGuard API until the rewrite appears or timeout is reached.

    Args:
        dc_func: Function to run docker compose commands
        domain: Domain name to look for
        expected_answer: If provided, also verify the answer matches
        timeout_seconds: Maximum time to wait
        poll_seconds: Interval between checks

    Returns:
        The rewrite entry dict if found, None if timeout reached
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        rewrites = _get_adguard_rewrites(dc_func)
        for entry in rewrites:
            if entry.get("domain") == domain:
                if expected_answer is None:
                    return entry
                if str(entry.get("answer", "")).strip() == expected_answer:
                    return entry
        time.sleep(poll_seconds)
    return None


def _wait_for_router(
    dc_func,
    router_name: str,
    timeout_seconds: int = 60,
    poll_seconds: float = 2.0,
) -> dict[str, Any] | None:
    """Wait for a Traefik router to appear.

    Polls the Traefik API until the router appears or timeout is reached.

    Args:
        dc_func: Function to run docker compose commands
        router_name: Name of the router to look for (e.g., "whoami-internal@docker")
        timeout_seconds: Maximum time to wait
        poll_seconds: Interval between checks

    Returns:
        The router entry dict if found, None if timeout reached
    """
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        routers = _get_traefik_routers(dc_func)
        for router in routers:
            if router.get("name") == router_name:
                return router
        time.sleep(poll_seconds)
    return None


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.mark.integration
def test_local_stack_syncs_traefik_routes_to_adguard() -> None:
    """Test that external-dns syncs Traefik routes to AdGuard DNS rewrites.

    This is the primary integration test that verifies the complete end-to-end flow:

    1. **Setup**: Build Docker image and start compose stack
    2. **Health Check**: Wait for external-dns container to become healthy
    3. **Router Discovery**: Verify Traefik API has the expected router
    4. **DNS Sync**: Poll AdGuard until the DNS rewrite appears
    5. **Validation**: Confirm correct domain and IP mapping

    Test Scenarios Covered:
    - Route added -> DNS record appears (primary scenario)
    - Sync is idempotent (external-dns runs multiple polls during test)
    - Correct target IP from config is used

    Prerequisites:
    - Docker must be installed and running
    - EXTERNAL_DNS_RUN_DOCKER_TESTS=1 environment variable must be set
    """
    if os.getenv("EXTERNAL_DNS_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set EXTERNAL_DNS_RUN_DOCKER_TESTS=1 to run Docker integration tests")

    if not _docker_available():
        pytest.skip("docker not available")

    repo = Path(__file__).resolve().parents[2]
    compose_file = repo / "docker-compose.yaml"

    _step(f"Using compose file: {compose_file}")

    # Use repo-local volumes/config for the local stack.
    # We bring it up if not already running, and only tear down if this test started it.
    env = os.environ.copy()
    env.setdefault("IMAGE", "external-dns:local")

    # Derive expected target IP from the same config external-dns reads.
    traefik_cfg = repo / "docker" / "local" / "external-dns" / "config" / "traefik-instances.yaml"
    cfg_obj = yaml.safe_load(traefik_cfg.read_text(encoding="utf-8"))
    expected_target_ip = str(cfg_obj["instances"][0]["target_ip"]).strip()

    def dc(*args: str) -> subprocess.CompletedProcess:
        """Run docker compose command with project context."""
        return _run(["docker", "compose", "-f", str(compose_file), *args], env=env)

    def dc_ok(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run docker compose command and assert success."""
        out = _run(["docker", "compose", "-f", str(compose_file), *args], env=env, timeout=timeout)
        assert out.returncode == 0, out.stdout
        return out

    # Build local image used by the compose stack.
    _step("Building local Docker image: external-dns:local")
    build = _run(["docker", "build", "-q", "-t", "external-dns:local", str(repo)])
    assert build.returncode == 0, build.stdout

    started_stack = False
    try:
        # Detect whether the stack is already running.
        ps_before = dc_ok("ps")
        ps_text_before = ps_before.stdout
        already_running = all(
            name in ps_text_before and "Up" in ps_text_before
            for name in ("adguard", "traefik", "whoami", "external-dns")
        )

        if already_running:
            _step("Local stack already running; reusing it")
        else:
            _step("Seeding local test data directories")
            conf_target = repo / "docker" / "local" / "adguard" / "conf" / "AdGuardHome.yaml"
            conf_example = (
                repo / "docker" / "local" / "adguard" / "conf.example" / "AdGuardHome.yaml"
            )
            adguard_work_dir = repo / "docker" / "local" / "adguard" / "work"
            external_dns_data_dir = repo / "docker" / "local" / "external-dns" / "data"

            conf_target.parent.mkdir(parents=True, exist_ok=True)
            conf_target.write_text(conf_example.read_text(encoding="utf-8"), encoding="utf-8")

            if adguard_work_dir.exists():
                shutil.rmtree(adguard_work_dir)
            adguard_work_dir.mkdir(parents=True, exist_ok=True)

            # Ensure external-dns doesn't reuse stale state.
            if external_dns_data_dir.exists():
                shutil.rmtree(external_dns_data_dir)
            external_dns_data_dir.mkdir(parents=True, exist_ok=True)

            _step("Starting local stack (idempotent)")
            dc_ok("up", "-d", "--no-build")
            started_stack = True

        # -------------------------------------------------------------------
        # Scenario: Validate containers are running
        # -------------------------------------------------------------------
        _step("Validating containers are running")
        ps_out = dc_ok("ps")
        ps_text = ps_out.stdout
        for service_name in ("adguard", "traefik", "whoami", "external-dns"):
            assert (
                service_name in ps_text
            ), f"Missing service in `docker compose ps`: {service_name}\n{ps_text}"

        # -------------------------------------------------------------------
        # Scenario: external-dns health check
        # -------------------------------------------------------------------
        _step("Waiting for external-dns container health")
        health = _wait_for_container_health("external-dns", timeout_seconds=60)
        if health != "healthy":
            _step("external-dns did not become healthy; dumping status and logs")
            ps_out = dc("ps")
            logs_all = dc("logs", "--no-color", "--tail", "200")
            raise AssertionError(
                f"external-dns health was '{health}' (expected 'healthy')\n"
                f"compose ps:\n{ps_out.stdout}\n\n"
                f"compose logs (tail):\n{logs_all.stdout}"
            )

        # -------------------------------------------------------------------
        # Scenario: Route added -> Traefik router appears
        # -------------------------------------------------------------------
        _step("Checking Traefik API for expected router")
        router = _wait_for_router(dc, "whoami-internal@docker", timeout_seconds=60)
        assert router is not None, "Expected Traefik router 'whoami-internal@docker' not found"
        _step(f"Found router: {router.get('name')}")

        # -------------------------------------------------------------------
        # Scenario: Route added -> DNS record appears
        # -------------------------------------------------------------------
        _step("Checking AdGuard rewrites for expected DNS entry")
        rewrite = _wait_for_rewrite(
            dc, "whoami-internal.localtest.me", expected_target_ip, timeout_seconds=90
        )
        assert (
            rewrite is not None
        ), "Expected DNS rewrite for 'whoami-internal.localtest.me' not found"

        # Validate the answer matches expected target IP
        found_answer = str(rewrite.get("answer", "")).strip()
        assert (
            found_answer == expected_target_ip
        ), f"Rewrite had unexpected answer. expected={expected_target_ip} got={found_answer}"
        _step(f"Found rewrite: {rewrite.get('domain')} -> {rewrite.get('answer')}")

        # -------------------------------------------------------------------
        # Scenario: Sync produces expected log output
        # -------------------------------------------------------------------
        _step("Confirming external-dns reported adding the record")
        logs = dc_ok("logs", "--no-color", "--tail", "200", "external-dns")
        assert "Adding record whoami-internal.localtest.me" in logs.stdout, logs.stdout

        # -------------------------------------------------------------------
        # Scenario: Idempotent sync (implicit - multiple poll cycles ran)
        # -------------------------------------------------------------------
        # Note: The test takes ~60-90 seconds, during which external-dns
        # runs multiple sync cycles. If duplicates were created or records
        # were incorrectly removed, the assertions above would fail.
        _step("Verifying sync is idempotent (no duplicate records)")
        rewrites = _get_adguard_rewrites(dc)
        matching = [r for r in rewrites if r.get("domain") == "whoami-internal.localtest.me"]
        assert len(matching) == 1, f"Expected exactly 1 rewrite, found {len(matching)}: {matching}"

        _step("All validations passed")

    finally:
        if started_stack:
            _step("Tearing down stack started by this test")
            dc("down", "-v")
        else:
            _step("Leaving pre-existing local stack running")
