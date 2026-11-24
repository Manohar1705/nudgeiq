from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str

    class Config:
        env_file = ".env"

settings = Settings()

print(f"DATABASE_URL from settings: {settings.DATABASE_URL}")