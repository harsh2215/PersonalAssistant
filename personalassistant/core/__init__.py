"""
core/ — Infrastructure and shared services used by all agents.

Modules:
  data_source_adapter  — DataSourceAdapter ABC + DirectAPIAdapter
  user_registry        — user CRUD and terminal Google OAuth onboarding
  state_store          — per-user alert-crossing state (alert_state table)
  history_store        — per-user time-series data (stock_history,
                         calendar_events tables)
  config               — .env loading + shared constants (single load_dotenv)
  bootstrap            — logging setup + idempotent table creation
  discord_notifier     — outbound alert DMs (REST API) + message splitting
  discord_listener     — inbound DM routing to chat_agent
"""
