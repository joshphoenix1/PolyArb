from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Wallet
    private_key: str = Field(..., description="Polygon wallet private key")

    # API endpoints
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    ws_host: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Chain
    chain_id: int = 137

    # Trading parameters
    min_profit_threshold: float = 0.02
    max_position_size: float = 500.0
    max_total_exposure: float = 5000.0
    daily_loss_limit: float = 200.0

    # Scanning
    scan_interval: int = 60

    # Dashboard
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080

    # Paper trading
    paper_trading: bool = True

    # Stale data threshold (seconds) - applied only when an opportunity is detected
    # The executor always re-verifies via REST before placing orders
    stale_data_threshold: float = 30.0

    # Min liquidity (open interest in USDC) to consider a market
    min_liquidity: float = 100.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
