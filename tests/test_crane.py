import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dockerhub_cleanup.crane import (
    CommandResult,
    CraneClient,
    SubprocessRunner,
    resolve_crane_command,
)
from dockerhub_cleanup.errors import CleanupError


class FakeRunner:
    def __init__(self, results: list[CommandResult | Exception]):
        self.results = results
        self.calls: list[tuple[list[str], str | None, Mapping[str, str]]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None,
        env: Mapping[str, str],
    ) -> CommandResult:
        self.calls.append((list(args), input_text, env))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def ok() -> CommandResult:
    return CommandResult(0, "", "")


def test_resolve_crane_command_prefers_mise() -> None:
    assert resolve_crane_command(lambda name: f"/bin/{name}") == (
        "mise",
        "exec",
        "--",
        "crane",
    )


def test_resolve_crane_command_falls_back_to_binary() -> None:
    assert resolve_crane_command(lambda name: "/bin/crane" if name == "crane" else None) == (
        "crane",
    )


def test_resolve_crane_command_requires_an_installation() -> None:
    with pytest.raises(CleanupError, match="unavailable"):
        resolve_crane_command(lambda _name: None)


def test_resolve_crane_command_looks_up_runtime_default() -> None:
    with patch("dockerhub_cleanup.crane.shutil.which", return_value="/bin/mise") as which:
        assert resolve_crane_command() == ("mise", "exec", "--", "crane")
    which.assert_called_once_with("mise")


def test_client_logs_in_with_stdin_and_deletes_digest() -> None:
    runner = FakeRunner([ok(), ok()])
    client = CraneClient("user", "pat", runner=runner, command=("crane",))
    config_path = Path(client.docker_config)

    assert config_path.is_dir()
    login_args, login_input, login_env = runner.calls[0]
    assert login_args == [
        "crane",
        "auth",
        "login",
        "index.docker.io",
        "-u",
        "user",
        "--password-stdin",
    ]
    assert login_input == "pat"
    assert login_env["DOCKER_CONFIG"] == str(config_path)

    client.delete_digest("user", "app", "sha256:abc")
    delete_args, delete_input, delete_env = runner.calls[1]
    assert delete_args == [
        "crane",
        "delete",
        "index.docker.io/user/app@sha256:abc",
    ]
    assert delete_input is None
    assert delete_env["DOCKER_CONFIG"] == str(config_path)

    client.close()
    assert not config_path.exists()


def test_context_manager_removes_temporary_config() -> None:
    runner = FakeRunner([ok()])
    with CraneClient("user", "pat", runner=runner, command=("crane",)) as client:
        config_path = Path(client.docker_config)
        assert config_path.exists()
    assert not config_path.exists()


def test_login_failure_cleans_up_and_redacts_pat() -> None:
    runner = FakeRunner([CommandResult(1, "", "invalid pat-secret")])
    with pytest.raises(CleanupError, match="<redacted>") as raised:
        CraneClient("user", "pat-secret", runner=runner, command=("crane",))
    assert "pat-secret" not in str(raised.value)
    config_path = Path(runner.calls[0][2]["DOCKER_CONFIG"])
    assert not config_path.exists()


def test_login_failure_handles_empty_secret() -> None:
    runner = FakeRunner([CommandResult(1, "", "invalid")])
    with pytest.raises(CleanupError, match="invalid"):
        CraneClient("user", "", runner=runner, command=("crane",))


def test_login_exception_cleans_up_temporary_config() -> None:
    runner = FakeRunner([CleanupError("runner failed")])

    with pytest.raises(CleanupError, match="runner failed"):
        CraneClient("user", "pat", runner=runner, command=("crane",))

    config_path = Path(runner.calls[0][2]["DOCKER_CONFIG"])
    assert not config_path.exists()


def test_delete_failure_reports_reference() -> None:
    runner = FakeRunner([ok(), CommandResult(1, "", "still referenced")])
    with (
        CraneClient("user", "pat", runner=runner, command=("crane",)) as client,
        pytest.raises(CleanupError, match="user/app@sha256:abc.*still referenced"),
    ):
        client.delete_digest("user", "app", "sha256:abc")


def test_subprocess_runner_captures_result() -> None:
    completed = subprocess.CompletedProcess(["command"], 2, stdout="out", stderr="err")
    with patch("subprocess.run", return_value=completed) as run:
        result = SubprocessRunner().run(
            ["command", "arg"], input_text="input", env={"PATH": os.defpath}
        )

    assert result == CommandResult(2, "out", "err")
    assert run.call_args.args == (["command", "arg"],)
    assert run.call_args.kwargs == {
        "input": "input",
        "text": True,
        "capture_output": True,
        "env": {"PATH": os.defpath},
        "check": False,
    }


def test_subprocess_runner_reports_start_failure() -> None:
    with (
        patch("subprocess.run", side_effect=FileNotFoundError("missing")),
        pytest.raises(CleanupError, match="could not start command.*missing"),
    ):
        SubprocessRunner().run(["command"], input_text=None, env={})


def test_default_runner_executes_resolved_command() -> None:
    completed = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch(
            "dockerhub_cleanup.crane.resolve_crane_command",
            return_value=("resolved-crane",),
        ) as resolve,
        patch("subprocess.run", return_value=completed) as run,
        CraneClient("user", "pat") as client,
    ):
        assert client.docker_config
    resolve.assert_called_once_with()
    assert run.call_args.args[0][:2] == ["resolved-crane", "auth"]
