import json
import subprocess
import sys
from pathlib import Path


def test_unconfigured_access_status_works_without_container_or_dns_binaries(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "portmap.cli", "status", "--check"],
        env={
            "HOME": str(home),
            "PATH": "",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PORTMAP_ROOT": str(tmp_path / "config"),
            "PORTMAP_STATE_DIR": str(tmp_path / "state"),
            "PORTMAP_RUNTIME_SOCKET": str(tmp_path / "missing-container-runtime.sock"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["connections"] == []
    assert status["server"]["configured"] is False
    assert status["gateway"]["running"] is False
    assert status["dns"]["running"] is False
