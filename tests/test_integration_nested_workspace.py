"""Opt-in nested-workspace integration test.

This test never runs in the default suite: it requires BOTH the
``integration`` marker selection and ``PORTMAP_RUN_NESTED_INTEGRATION=1``,
plus a verification VM operator setup (local outer Podman usable ROOTLESS
by an ordinary user with a subuid range; see the driver docstring). It
drives the closed ``prepare``/``exercise``/``cleanup`` driver interface and
asserts the receipts: every acceptance-critical check must be present and
passing in each role receipt, and the cleanup receipt must show exact-ID
removal with the host baseline (DNS/NSS files, forwarding sysctls, default
engine stores, preexisting user namespaces) preserved.
"""
from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

PHASE_TIMEOUTS = {"prepare": 2400, "exercise": 8400, "cleanup": 900}

WORKSPACE_CHECKS = (
    "pid1_is_tini",
    "system_tools_absent",
    "uid_1000",
    "userns_65536_identity_coverage",
    "ordinary_caps_zero_bound_profile",
    "sudo_capped_to_profile",
    "no_new_privs_off_seccomp_off",
    "apparmor_unconfined",
    "dev_fuse_present",
    "namespace_lowport_zero",
    "root_owned_bind_resolver_0644",
    "split_subid_ranges",
    "nss_hosts_files_dns",
    "inner_podman_4_9_rootless",
    "inner_overlay_storage",
    "inner_cgroupfs_events_file",
    "inner_real_container_runs",
    "engine_data_on_workspace_bind",
    "engine_graphroot_on_workspace_bind",
    "resolver_forward_taken_over",
    "resolver_search_options_preserved",
    "no_host_engine_socket_mounted",
    "resolver_bind_inode_stable",
    "resolver_owner_preserved",
    "status_reports_forward_installed",
    "os_resolves_portmap_domain",
    "dns_udp_a_loopback",
    "dns_tcp_a_loopback",
    "dns_aaaa_nodata",
    "dns_nxdomain_outside_zone",
    "dns_project_host_a",
    "dns_original_upstream_forwarded",
    "dns_original_upstream_aaaa_forwarded",
    "os_resolves_project_host",
    "raw_udp_roundtrip",
    "raw_udp_range_base",
    "raw_udp_range_member",
    "container_side_dns_and_http",
    "ssh_master_owned_by_supervised_worker",
    "peer_endpoint_reachable",
    "saved_connection_config_retained",
    "boundary_private_lists_peer",
    "boundary_public_local_only",
    "boundary_connections_200_private_404_public",
    "boundary_peer_machine_200_private_404_public",
    "boundary_remote_host_routes_private_only",
    "run_sigterm_clean_exit",
    "agent_worker_gone",
    "ssh_masters_gone",
    "shared_infrastructure_retained",
    "private_entry_retained_after_sigterm",
    "saved_profiles_retained_after_sigterm",
    "peer_mapping_withdrawn_after_sigterm",
    "restart_restores_peer_mapping",
    "restart_revives_owned_master",
    "restart_sigterm_clean_exit",
    "demo_one_shot_setup_ok",
    "host_access_teardown_ok",
    "host_access_resolver_restored_exactly",
    "teardown_removes_remote_connections",
    "consumer_coredns_same_instance_retained",
    "host_53_publication_retained_with_consumers",
    "container_dns_after_host_release",
    "project_serving_after_host_release",
    "portmap_down_ok",
    "loopback_53_released_final",
    "final_teardown_ok",
    "loopback_80_released_final",
    "resolver_original_at_end",
    "infrastructure_containers_gone",
    "ssh_masters_gone_final",
    "reinit_resolver_taken_over",
    "reinit_connection_reestablished",
    "reinit_master_supervised",
    "reinit_sigterm_clean_exit",
    "reinit_profiles_retained_no_teardown",
    "reinit_infrastructure_retained_no_teardown",
)

PEER_CHECKS = (
    "peer_has_no_sudo",
    "server_only_up_with_53_busy",
    "server_only_no_host_dns_takeover",
    "server_only_publishes_http",
    "server_only_container_dns_supplied",
    "server_only_down_ok",
    "server_only_resolver_unchanged_at_end",
    "engine_data_on_workspace_bind",
    "engine_graphroot_on_workspace_bind",
    "inner_podman_4_9_rootless",
    "inner_overlay_storage",
    "inner_real_container_runs",
)

RECREATED_CHECKS = (
    "recreated_resolver_identity_is_new",
    "engine_data_on_workspace_bind",
    "engine_graphroot_on_workspace_bind",
    "recreated_saved_connection_retained",
    "recreated_connection_restored",
    "recreated_restored_master_supervised",
    "recreated_takes_over_current_file",
    "recreated_sigterm_clean_exit",
    "recreated_owned_masters_gone",
    "recreated_teardown_ok",
    "recreated_restores_fresh_backup",
    "recreated_ports_released",
)


def test_nested_workspace_pod_runs_portmap_with_inner_rootless_podman(tmp_path: Path) -> None:
    if os.environ.get("PORTMAP_RUN_NESTED_INTEGRATION") != "1":
        pytest.skip("set PORTMAP_RUN_NESTED_INTEGRATION=1 to run the nested-workspace Podman fixture")
    operator = os.environ.get("PORTMAP_NESTED_OPERATOR", "debian")
    try:
        operator_entry = pwd.getpwnam(operator)
    except KeyError:
        pytest.skip(f"operator user {operator!r} does not exist on this VM")
    current = os.geteuid()
    if current != operator_entry.pw_uid and current != 0:
        pytest.skip(f"run as root or as the operator user {operator!r}")
    # The scratch tree must be writable by the operator: root-owned pytest
    # tmp dirs are not, so relocate under the operator's home.
    base = Path(tempfile.mkdtemp(prefix="portmap-nested-pytest-", dir=operator_entry.pw_dir))
    state_dir = base / "state"
    out_dir = base / "out"

    prefix: list[str] = []
    if current == 0:
        prefix = ["sudo", "-u", operator, "--"]
        os.chown(base, operator_entry.pw_uid, operator_entry.pw_gid)

    def driver(command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*prefix, sys.executable, "-m", "tests.fixtures.nested_workspace.driver", command,
             "--state-dir", str(state_dir), "--out-dir", str(out_dir)],
            cwd=str(Path(__file__).resolve().parents[1]), text=True, capture_output=True,
            timeout=PHASE_TIMEOUTS[command],
        )

    cleanup_result = None
    try:
        prepare = driver("prepare")
        assert prepare.returncode == 0, f"prepare failed:\n{prepare.stdout}\n{prepare.stderr}"
        receipt = json.loads((out_dir / "prepare.json").read_text(encoding="utf-8"))
        assert receipt["ok"] is True, receipt.get("error")
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["outer"]["mode"] == "rootless" and manifest["outer"]["uplink"] in ("pasta", "slirp4netns")
        assert manifest["expected_cap_bnd"] == "00000000802001db"
        for role in ("workspace", "peer", "dns"):
            assert manifest["containers"][role]["id"]

        exercise = driver("exercise")
        exercise_receipt = json.loads((out_dir / "exercise.json").read_text(encoding="utf-8"))
        expectations = {
            "exercise-workspace.json": WORKSPACE_CHECKS,
            "exercise-recreated.json": RECREATED_CHECKS,
        }
        details = {}
        for name, required in expectations.items():
            payload = json.loads((out_dir / name).read_text(encoding="utf-8"))
            checks = payload.get("checks", {})
            details[name] = checks
            missing = [item for item in required if item not in checks]
            failed = [item for item, result in checks.items() if not result.get("ok")]
            assert missing == [], f"{name}: acceptance checks missing: {missing}"
            assert failed == [], f"{name}: failed checks: {failed}"
        peer_final = json.loads((out_dir / "peer-final.json").read_text(encoding="utf-8"))
        peer_checks = peer_final.get("checks", {})
        missing = [item for item in PEER_CHECKS if item not in peer_checks]
        failed = [item for item, result in peer_checks.items() if not result.get("ok")]
        assert missing == [], f"peer: acceptance checks missing: {missing}"
        assert failed == [], f"peer: failed checks: {failed}"
        assert exercise.returncode == 0, (
            f"exercise failed:\n{exercise.stdout}\n{exercise.stderr}\n"
            f"receipt: {json.dumps(exercise_receipt, indent=2)[:4000]}"
        )
        incoming = exercise_receipt["phases"]["incoming_ssh"]
        assert incoming["returncode"] == 0 and incoming["stdout"].splitlines()[0] == "1000", incoming
        workspace_receipt = json.loads((out_dir / "exercise-workspace.json").read_text(encoding="utf-8"))
        assert workspace_receipt.get("proof_mode", "").startswith("source-mode")
        assert "no backend flags" in workspace_receipt["proof_mode"]
    finally:
        cleanup_result = driver("cleanup")

    cleanup_receipt = json.loads((out_dir / "cleanup.json").read_text(encoding="utf-8"))
    assert cleanup_result.returncode == 0, f"cleanup failed:\n{cleanup_result.stdout}\n{cleanup_result.stderr}"
    assert cleanup_receipt["ok"] is True, cleanup_receipt
    assert cleanup_receipt["baseline_comparison"]["files_preserved"] is True
    assert cleanup_receipt["baseline_comparison"]["sysctls_preserved"] is True
    assert cleanup_receipt["baseline_comparison"]["forwarding_policy_preserved"] is True
    assert cleanup_receipt["baseline_comparison"]["preexisting_userns_preserved"] is True
