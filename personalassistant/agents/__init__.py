"""
agents/ — Contains all monitoring and reasoning components.

Modules:
  stocks_monitor   — deterministic stock-price polling and alerting
  calendar_monitor — deterministic Google Calendar polling and near-term event alerting
  chat_agent       — LLM-driven (Gemini) question-answering over stored data
                     via the google-genai SDK; uses function calling to ground
                     answers in the user's SQLite-stored stock and calendar data
"""
