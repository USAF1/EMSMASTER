# master_config.py
# Master (ESP32-S3) — Pin Definitions and Default Configuration
# Innovatsii EMS — Pico 1
# Firmware Version: 0.3.0
#
# NOTE (Sensor Hub v0.3+): The Hub no longer sends the "environment" message
# or temperature/humidity fields. Neither the ZG-204ZL PIR nor the ZG-102Z/ZA
# door sensors report temp/hum. The Master ignores any environment messages.
# The PIR (ZG-204ZL) accepts keep_time (fading) and sensitivity config.
#   keep_time_sec : one of 10/30/60/120 (mapped by the Hub to DP10 enum)
#   sensitivity   : 0=low, 1=medium, 2=high (PIR DP9 range is 0..2)

FIRMWARE_VERSION   = "0.3.0"
FIRMWARE_COMPONENT = "master"

SENSOR_UART_ID   = 1
SENSOR_UART_TX   = 16
SENSOR_UART_RX   = 17
SENSOR_UART_BAUD = 9600

SCHED_UART_ID   = 2
SCHED_UART_TX   = 18
SCHED_UART_RX   = 21
SCHED_UART_BAUD = 9600

CONFIG_FILE = "master_config.json"

DEFAULT_CONFIG = {
    "tenant_id":             "default_tenant",
    "unit_id":               "default_unit",
    "mode":                  "production",
    "wifi_ssid":             "",
    "wifi_password":         "",
    "mqtt_broker":           "",
    "mqtt_port":             1883,
    "mqtt_username":         "",
    "mqtt_password":         "",
    "mqtt_enabled":          False,
    "check_in_utc":          "2000-01-01 00:00:00",
    "check_out_utc":         "2000-01-01 00:00:00",
    "buffer_minutes":        15,
    "last_sensor_status":    "vacant",
    "last_decided_status":   "Vacant",
    "last_scheduler_status": None,

    "hub_status": {
        "known":          False,
        "last_boot_utc":  "",
        "sensor_count":   0,
        "fault":          False,
        "fault_reason":   ""
    },

    "force": {
        "active":        False,
        "status":        "",
        "expires_utc":   "",
        "expires_epoch": 0,
        "reason":        ""
    },

    "pzem_last_readings": {
        "MAIN": 0.0, "R1M": 0.0, "R2M": 0.0, "R3M": 0.0, "R4M": 0.0,
        "R5M": 0.0, "R6M": 0.0, "R7M": 0.0, "R8M": 0.0
    },

    "sensor_hub_config": {
        "pairing_duration_sec":           120,
        "watchdog_enable":                True,
        "watchdog_interval_min":          60,
        "watchdog_ping_timeout_sec":      30,
        "door_alarm_threshold_min":       10,
        "heartbeat_interval_min":         30,
        "presence_fading_time_sec":       30,     # ZG-204ZL keep_time default (snaps to 10/30/60/120)
        "motion_sensitivity":             1,      # ZG-204ZL sensitivity default (0=low,1=med,2=high)
        "door_sensor_max_silence_hours":  24,
        "sensor_names":                   {}
    }
}
