from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    QDRANT_URL: str
    LLM_PROVIDER: str = "local"
    LLM_BASE_URL: str
    LLM_API_KEY: str
    LLM_MODEL: str
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_BASE_URL: str
    EMBEDDING_MODEL: str
    KB_DATA_DIR: str = "/data/kb"
    SESSION_TTL_DAYS: int = 7
    RATE_LIMIT_PER_HOUR: int = 100
    ADMIN_EMAIL: str
    ADMIN_PASSWORD: str
    
    class Config:
        env_file = ".env"
        extra = "ignore"
