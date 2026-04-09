"""
Configuration management for Gemini API Server
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings"""
    
    # API Configuration
    api_title: str = "Gemini API Server"
    api_version: str = "1.0.0"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False
    
    # Gemini Configuration
    gemini_psid: Optional[str] = None
    gemini_psidts: Optional[str] = ""
    gemini_cookie_path: Optional[str] = None
    gemini_proxy: Optional[str] = None
    
    # Client Configuration  
    client_timeout: int = 30
    client_auto_close: bool = True
    client_close_delay: int = 300
    client_auto_refresh: bool = True
    
    # Request Configuration
    request_timeout: int = 300
    max_upload_size: int = 100 * 1024 * 1024  # 100MB
    
    class Config:
        env_file = ".env"
        env_prefix = ""
        case_sensitive = False


settings = Settings()
