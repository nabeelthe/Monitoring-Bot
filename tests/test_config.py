import textwrap

from nuva_bot.config import Config


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "sekrit")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(textwrap.dedent("""
        a:
          token: "${MY_TOKEN}"
          fallback: "${MISSING_VAR:-dflt}"
          empty: "${MISSING_VAR}"
        list:
          - "${MY_TOKEN}"
    """))
    cfg = Config.load(cfg_file)
    assert cfg.get("a.token") == "sekrit"
    assert cfg.get("a.fallback") == "dflt"
    assert cfg.get("a.empty") == ""
    assert cfg.getlist("list") == ["sekrit"]


def test_dotted_access_and_defaults():
    cfg = Config({"x": {"y": {"z": 3}}, "flag": "true", "n": "7.5"})
    assert cfg.get("x.y.z") == 3
    assert cfg.get("x.missing", "d") == "d"
    assert cfg.getbool("flag")
    assert not cfg.getbool("nope")
    assert cfg.getfloat("n") == 7.5
    assert cfg.getint("n") == 7
    assert cfg.section("x.y") == {"z": 3}
    assert cfg.section("flag") == {}


def test_real_config_loads_and_builds_monitors(monkeypatch):
    # the shipped config.yaml must always parse and build cleanly
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    from nuva_bot.monitors import build_monitors
    cfg = Config.load("config.yaml")
    monitors = build_monitors(cfg)
    names = {m.name for m in monitors}
    assert "Provenance Explorer" in names
    assert "CoinGecko HASH" in names
    assert "X (Twitter)" in names
    assert "News" in names
    # gated monitors must be off without credentials
    assert "Ethereum / NUVA contracts" not in names
    assert "Osmosis HASH/OSMO pool" not in names
