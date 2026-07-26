# master_config.py
# Master (ESP32-S3) — Pin Definitions and Default Configuration
# Innovatsii EMS — Pico 1
#
# UART1 (Master <-> Sensor Hub):  TX=GPIO16  RX=GPIO17
# UART2 (Master <-> Scheduler):   NOT INITIALISED — Phase 4

# ============================================================================
# UART PIN DEFINITIONS
# ============================================================================

SENSOR_UART_ID   = 1
SENSOR_UART_TX   = 16     # GPIO16 → Sensor Hub RX (GPIO5)
SENSOR_UART_RX   = 17     # GPIO17 ← Sensor Hub TX (GPIO4)
SENSOR_UART_BAUD = 9600   # Reduced from 115200 for EMI noise immunity

# Scheduler UART pins defined but UART not initialised until Phase 4
SCHED_UART_ID   = 2
SCHED_UART_TX   = 18
SCHED_UART_RX   = 21
SCHED_UART_BAUD = 9600

# ============================================================================
# FILE PATHS
# ============================================================================

CONFIG_FILE = "master_config.json"

# ============================================================================
# DEFAULT CONFIGURATION
# ============================================================================

DEFAULT_CONFIG = {
    "tenant_id":            "default_tenant",
    "unit_id":              "default_unit",
    "wifi_ssid":            "",
    "wifi_password":        "",
    "mqtt_broker":          "",
    "mqtt_port":            1883,
    "mqtt_username":        "",
    "mqtt_password":        "",
    "mqtt_enabled":         False,
    "check_in_utc":         "2000-01-01 00:00:00",
    "check_out_utc":        "2000-01-01 00:00:00",
    "buffer_minutes":       15,
    "use_manual_utc_now":   False,
    "manual_utc_now":       "2000-01-01 00:00:00",
    "last_sensor_status":   "vacant",
    "last_decided_status":  "Vacant",
    "last_scheduler_status": None,
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