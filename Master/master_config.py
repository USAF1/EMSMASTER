# master_config.py
# Master (ESP32-S3) — Pin Definitions and Default Configuration
# Innovatsii EMS — Pico 1
#
# UART1 (Master <-> Sensor Hub):  TX=GPIO16  RX=GPIO17
# UART2 (Master <-> Scheduler):   TX=GPIO18  RX=GPIO21

# ============================================================================
# UART PIN DEFINITIONS
# ============================================================================

SENSOR_UART_ID = 1
SENSOR_UART_TX = 4
SENSOR_UART_RX = 5
SENSOR_UART_BAUD = 115200

SCHED_UART_ID = 2
SCHED_UART_TX = 18
SCHED_UART_RX = 21
SCHED_UART_BAUD = 115200

# ============================================================================
# FILE PATHS
# ============================================================================

CONFIG_FILE = "master_config.json"

# ============================================================================
# DEFAULT CONFIGURATION
# ============================================================================

DEFAULT_CONFIG = {
    # Identity
    "tenant_id":            "default_tenant",
    "unit_id":              "default_unit",

    # Network
    "wifi_ssid":            "",
    "wifi_password":        "",

    # MQTT — Phase 6
    "mqtt_broker":          "",
    "mqtt_port":            1883,
    "mqtt_username":        "",
    "mqtt_password":        "",
    "mqtt_enabled":         False,

    # Booking window — UTC datetime strings
    "check_in_utc":         "2000-01-01 00:00:00",
    "check_out_utc":        "2000-01-01 00:00:00",

    # Buffer period in minutes
    "buffer_minutes":       15,

    # Manual UTC clock override — kept for offline testing fallback
    "use_manual_utc_now":   False,
    "manual_utc_now":       "2000-01-01 00:00:00",

    # Persisted runtime state
    "last_sensor_status":   "vacant",
    "last_decided_status":  "Vacant",
    "last_scheduler_status": None,

    # PZEM meter last readings — Phase 5
    "pzem_last_readings": {
        "MAIN": 0.0,
        "R1M":  0.0,
        "R2M":  0.0,
        "R3M":  0.0,
        "R4M":  0.0,
        "R5M":  0.0,
        "R6M":  0.0,
        "R7M":  0.0,
        "R8M":  0.0
    },

    # Sensor Hub configurable parameters
    "sensor_hub_config": {
        "pairing_duration_sec":      120,
        "watchdog_enable":           True,
        "watchdog_interval_min":     60,
        "watchdog_ping_timeout_sec": 30,
        "door_alarm_threshold_min":  10,
        "heartbeat_interval_min":    30,
        "sensor_names": {}
    }
}
