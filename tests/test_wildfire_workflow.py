import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("slug,refresh_hotspots,expected_commands", [
    ("sumatra-2026", "true", 3), ("sumatra-2026", "false", 2),
    ("other-indonesia-2026", "true", 15), ("other-indonesia-2026", "false", 10),
])
def test_remote_docker_commands_cannot_consume_remaining_ssh_script(tmp_path, slug, refresh_hotspots, expected_commands):
    bash = shutil.which("bash")
    if os.name == "nt":
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if git_bash.is_file() else None
    if not bash:
        pytest.skip("Bash is unavailable")
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/refresh-wildfire-data.yml"
    source = workflow.read_text(encoding="utf-8")
    remote = source.split("<<'REMOTE_SYNC'\n", 1)[1].split("          REMOTE_SYNC", 1)[0]
    calls = tmp_path / "docker-calls"
    # The mock deliberately drains stdin, matching interactive docker exec.
    launcher = (
        'docker() { cat >/dev/null; printf x >> "$TASK_DOCKER_CALLS"; }; export -f docker; '
        'bash -s -- . ' + slug + ' 2026-08-01 2026-10-07 2026-09-29 2026-10-06 ' + refresh_hotspots
    )
    result = subprocess.run(
        [bash, "-c", launcher], input=textwrap.dedent(remote), text=True, capture_output=True,
        cwd=tmp_path, env={**os.environ, "TASK_DOCKER_CALLS": calls.as_posix()}, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert calls.read_text() == "x" * expected_commands
