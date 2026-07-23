from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wsl_proxy_stack_uses_local_images_before_pulling() -> None:
    script = (ROOT / "scripts" / "start_proxy_stack_wsl.sh").read_text(encoding="utf-8")

    assert "docker image inspect" in script
    assert "--pull never" in script


def test_windows_proxy_stack_prefers_systemd_docker_service() -> None:
    script = (ROOT / "scripts" / "start_proxy_stack.ps1").read_text(encoding="utf-8")

    assert "systemctl" in script
    assert "start docker" in script


def test_windows_proxy_stack_keeps_wsl_alive_after_startup() -> None:
    script = (ROOT / "scripts" / "start_proxy_stack.ps1").read_text(encoding="utf-8")

    assert "gptimage-wsl-keepalive" in script
    assert '"sleep",' in script
    assert '"infinity"' in script
    assert "-WindowStyle Hidden" in script


def test_stop_all_terminates_wsl_keepalive() -> None:
    script = (ROOT / "scripts" / "stop_all.ps1").read_text(encoding="utf-8")

    assert "gptimage-wsl-keepalive" in script
