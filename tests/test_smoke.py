import farerecord


def test_version() -> None:
    assert farerecord.__version__


def test_log_file_option_captures_output(tmp_path, monkeypatch) -> None:
    import sys

    from farerecord.cli import main

    monkeypatch.setattr(sys, "stdout", sys.stdout)  # main 会改写它们，测试结束后还原
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    config = tmp_path / "config.yaml"
    config.write_text("routes: []\n", encoding="utf-8")
    log = tmp_path / "logs" / "collect.log"
    assert main(["-c", str(config), "--log-file", str(log), "collect", "--if-due"]) == 2
    sys.stdout.close()
    text = log.read_text(encoding="utf-8")
    assert text.startswith("--- ") and "没有配置航线" in text
