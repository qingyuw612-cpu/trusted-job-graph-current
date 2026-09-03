"""LLM 适配器单元测试：并列 Switch 模式的环境变量解析 + 兼容别名（不联网）。"""

import pytest

from src.utils import llm as llm_module


_PROVIDER_ENV = (
    "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_EXTRA_BODY",
    "IFLYTEK_API_KEY", "IFLYTEK_BASE_URL", "IFLYTEK_MODEL", "IFLYTEK_EXTRA_BODY",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_EXTRA_BODY",
    "CUSTOM_API_KEY", "CUSTOM_BASE_URL", "CUSTOM_MODEL", "CUSTOM_EXTRA_BODY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "LLM_PROVIDER",
        "LLM_TIMEOUT",
        "LLM_MAX_RETRIES",
        *_PROVIDER_ENV,
        # 旧版共享变量已废弃：确认它们不再参与解析
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_EXTRA_BODY",
    ):
        monkeypatch.delenv(key, raising=False)


class TestSwitchMode:
    def test_deepseek_block_default_provider(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        cfg = llm_module.get_llm_config()
        assert cfg["provider"] == "deepseek"
        assert cfg["label"] == "DeepSeek"
        assert cfg["base_url"] == "https://api.deepseek.com/v1"  # 预设默认
        assert cfg["model"] == "deepseek-v4-flash"
        assert cfg["api_key"] == "sk-test"

    def test_deepseek_base_url_override(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        cfg = llm_module.get_llm_config()
        assert cfg["base_url"] == "https://api.deepseek.com"

    def test_switch_to_iflytek_block(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "iflytek")
        monkeypatch.setenv("IFLYTEK_API_KEY", "ak:sk")
        cfg = llm_module.get_llm_config()
        assert cfg["provider"] == "iflytek"
        assert cfg["label"] == "讯飞星火"
        assert cfg["base_url"] == "https://spark-api-open.xf-yun.com/v1"
        assert cfg["model"] == "4.0Ultra"
        # 并行块互不干扰：deepseek 块缺失不影响 iflytek

    def test_provider_blocks_are_isolated(self, monkeypatch):
        # 只配 deepseek，切到 iflytek 应报 IFLYTEK_API_KEY 缺失
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_PROVIDER", "iflytek")
        with pytest.raises(ValueError, match="IFLYTEK_API_KEY"):
            llm_module.get_llm_config()

    def test_missing_api_key_raises(self, monkeypatch):
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            llm_module.get_llm_config()

    def test_custom_requires_base_url_and_model(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "custom")
        monkeypatch.setenv("CUSTOM_API_KEY", "k")
        with pytest.raises(ValueError, match="CUSTOM_BASE_URL"):
            llm_module.get_llm_config()
        monkeypatch.setenv("CUSTOM_BASE_URL", "http://localhost:9009/v1")
        with pytest.raises(ValueError, match="CUSTOM_MODEL"):
            llm_module.get_llm_config()

    def test_unknown_provider_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "nope")
        with pytest.raises(ValueError, match="未知 LLM_PROVIDER"):
            llm_module.get_llm_config()

    def test_extra_body_must_be_valid_json(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "iflytek")
        monkeypatch.setenv("IFLYTEK_API_KEY", "k")
        monkeypatch.setenv("IFLYTEK_EXTRA_BODY", "{bad")
        with pytest.raises(ValueError, match="IFLYTEK_EXTRA_BODY"):
            llm_module.get_llm_config()

    def test_extra_body_parsed(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "iflytek")
        monkeypatch.setenv("IFLYTEK_API_KEY", "k")
        monkeypatch.setenv(
            "IFLYTEK_EXTRA_BODY", '{"thinking": {"type": "enabled"}}'
        )
        cfg = llm_module.get_llm_config()
        assert cfg["extra_body"] == {"thinking": {"type": "enabled"}}

    def test_legacy_shared_vars_ignored(self, monkeypatch):
        # 旧版 LLM_API_KEY / LLM_MODEL / LLM_BASE_URL 不再参与解析
        monkeypatch.setenv("LLM_API_KEY", "old")
        monkeypatch.setenv("LLM_MODEL", "old-model")
        monkeypatch.setenv("LLM_BASE_URL", "http://old")
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            llm_module.get_llm_config()

    def test_timeout_and_max_retries_defaults(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        cfg = llm_module.get_llm_config()
        assert cfg["timeout"] == 60.0
        assert cfg["max_retries"] == 2

    def test_call_deepseek_json_alias(self, monkeypatch):
        sentinel = {"ok": True}

        def fake(prompt, temperature=0.0):
            return sentinel

        monkeypatch.setattr(llm_module, "call_llm_json", fake)
        assert llm_module.call_deepseek_json("p") is sentinel

