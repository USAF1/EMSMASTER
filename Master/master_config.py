# master_config.py
# Master (ESP32-S3) — Pin Definitions and Configuration
# Innovatsii EMS — Pico 1
# Firmware Version: 0.3.0
#
# ****************************************************************************
# THIS FILE IS THE SINGLE SOURCE OF TRUTH.
#
# There is no master_config.json. DEFAULT_CONFIG below is loaded fresh at
# every boot, so whatever you set here is exactly what the Master runs.
# To change any setting: edit this file, copy it to the device, reset.
#
# Nothing persists across a reboot. Force commands, booking times received
# over MQTT, sensor renames and Wi-Fi changes made from the Debugger apply
# immediately but revert to the values below on power cycle.
# ****************************************************************************
#
# SENSOR SET: ZG-204ZL PIR (Tuya EF00) + ZG-102Z/ZA door (IAS Zone).
# Neither reports temperature or humidity — the 'environment' message and all
# temp/hum fields were removed from the protocol in Hub firmware 0.3.0.

FIRMWARE_VERSION   = "0.3.0"
FIRMWARE_VERSION   = "0.3.0"
FIRMWARE_COMPONENT = "master"

SENSOR_UART_ID   = 1
SENSOR_UART_TX   = 16
SENSOR_UART_RX   = 17
# 9600 is mandatory. GPIO5 on the ESP32-C6 runs beside the 802.15.4 radio
# traces; RF coupling during Zigbee TX bursts causes framing errors at 115200.
# Confirmed on live hardware 24 July 2026. Do not raise this.
SENSOR_UART_BAUD = 9600

SCHED_UART_ID   = 2
SCHED_UART_TX   = 18
SCHED_UART_RX   = 21
SCHED_UART_BAUD = 9600

DEFAULT_CONFIG = {
    "tenant_id":             "default_tenant",
    "unit_id":               "default_unit",
    "mode":                  "production",

    # ── Wi-Fi — the ONLY place credentials now live ─────────────────────────
    # An empty ssid means the Master boots into 2-state fallback: no network,
    # no NTP, and the Debugger cannot connect.
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

    # ── PIR occupancy tuning ────────────────────────────────────────────────
    # The ZG-204ZL is a motion sensor: it latches YES for keep_time after the
    # last movement, so at the moment a departing guest closes the door it is
    # always still YES. Vacancy is therefore decided by a confirmation window
    # that starts at the door close, not by sampling presence at that instant.
    #
    # vacancy_confirm_sec    how long after a door close we wait before
    #                        concluding the unit is empty. Must comfortably
    #                        exceed the PIR keep_time. Longer = safer against
    #                        a motionless occupant, slower to save energy.
    # motion_quiet_guard_sec margin added to keep_time before fresh motion is
    #                        trusted, so the departing guest's own latch is
    #                        not mistaken for someone still inside.
    "vacancy_confirm_sec":    180,
    "motion_quiet_guard_sec": 5,
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
        "presence_fading_time_sec":       30,     # ZG-204ZL DP10 keep_time: 10/30/60/120 only
        "motion_sensitivity":             1,      # ZG-204ZL DP9: 0=low, 1=medium, 2=high
        "door_sensor_max_silence_hours":  24,
        "sensor_names":                   {}
    }
}
