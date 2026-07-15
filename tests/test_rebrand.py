from pathlib import Path


def test_clean_rebrand_exposes_only_agentpermit_package():
    root = Path(__file__).resolve().parents[1]

    assert (root / "agentpermit" / "__main__.py").is_file()
    assert not (root / "agentops_control_plane").exists()


def test_cli_uses_agentpermit_brand_and_runtime_directory():
    from agentpermit import cli

    parser_text = Path(cli.__file__).read_text(encoding="utf-8")

    assert 'prog="agentpermit"' in parser_text
    assert "AgentPermit" in parser_text
    assert ".agentops" not in parser_text
