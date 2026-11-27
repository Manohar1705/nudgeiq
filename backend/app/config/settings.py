from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    This class defines the application's configuration settings.
    """
    model_config = SettingsConfigDict(
        # Use a relative path to find the .env file in the project root
        env_file='../.env',
        # All variables must start with "NUDGEIQ_"
        env_prefix="NUDGEIQ_",
        case_sensitive=False,
        # This is the important change: ignore variables we don't recognize
        extra='ignore'
    )

    # --- Define the settings the application needs ---

    # Pydantic will look for "NUDGEIQ_DATABASE_URL" in the .env file
    database_url: str

    # Pydantic will look for "NUDGEIQ_LLM" or use the default "mock"
    llm: str = "mock"

# Create a single, global instance of the settings
settings = Settings()