from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    telegram_bot_token: str
    telegram_api_id: int
    telegram_api_hash: str
    admin_telegram_id: int
    parser_session_name: str = "freelance_parser"
    database_path: str = "./freelance_parser.db"
    llm_api_key: str
    # Groq by default: OpenAI-compatible API, free tier.
    llm_base_url: str = "https://api.groq.com/openai/v1"
    # Verified working on Groq's free tier (llama models are Enterprise now).
    llm_model: str = "openai/gpt-oss-120b"
    log_level: str = "INFO"


_settings_cache: Settings | None = None


def load_settings() -> Settings:
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    env_path = Path(__file__).resolve().parents[1] / ".env"
    from dotenv import load_dotenv
    load_dotenv(env_path, override=False)
    _settings_cache = Settings()
    return _settings_cache


settings = load_settings()
