"""Constants for the SP Group integration."""

DOMAIN = "sp_group"

CONF_ACCOUNT_ID = "account_id"
CONF_APPROXIMATE_SEED_CONFIRM = "confirm_approximate_seed"
CONF_BACKFILL_FROM = "backfill_from"
CONF_DEV_AUTH_METHOD = "dev_auth_method"
CONF_DEV_SESSION_CONFIRM = "confirm_dev_cached_session"
CONF_ENABLED_UTILITIES = "enabled_utilities"
CONF_MAINTENANCE_ACTION = "maintenance_action"
CONF_NO_SOURCES_CONFIRM = "confirm_no_sources"
CONF_OTP = "otp"
CONF_ELECTRICITY_POLL_INTERVAL_HOURS = "electricity_poll_interval_hours"
CONF_REBUILD_LEDGER = "rebuild_ledger"
CONF_REBUILD_LEDGER_CONFIRM = "confirm_rebuild_ledger"
CONF_WATER_POLL_DAY = "water_poll_day"
CONF_WATER_POLL_TIME = "water_poll_time"
CONF_SESSION = "session"
CONF_SOURCE_CONFIG = "source_config"
CONF_SEED_OBSERVED_AT = "seed_observed_at"
CONF_SEED_VALUE = "seed_value"
CONF_RESET_REPLACEMENT = "confirm_reset_replacement"
CONF_STALE_FLOW_RESET = "confirm_stale_flow_reset"
DEFAULT_ELECTRICITY_POLL_INTERVAL_HOURS = 1
DEFAULT_WATER_POLL_DAY = 1
DEFAULT_WATER_POLL_TIME = "00:00:00"
DEFAULT_UPDATE_TIMEOUT_SECONDS = 30
PLATFORMS = ["sensor"]
