# config.py
CONFIG_FILE = "scheduler_config.json"

DEFAULT_CONFIG = {
    "currentStatus": "Occupied",
    "Scheduler": {
        "Occupied": {
            "R4": "On All time",
            "R5": "On All time",
            "R16": "On All time",
            "R17": "On All time",
            "R18": "On All time",
            "R21": "On All time",
            "R35": "On All time",
            "R36": "On All time",
            "R37": "On All time",
            "R38": "On All time"
        },
        "Sold Vacant": {
            "R4": "On All time",
            "R5": "5min On : 1min off",
            "R16":"2min On : 1min off",
            "R17":"3min On : 10min off",
            "R18":"4min On : 16min off",
            "R21":"5min On : 11min off",
            "R35":"15min On : 20min off",
            "R36":"25min On : 1min off",
            "R37":"5min On : 1min off",
            "R38":"5min On : 1min off"
        },
        "Vacant": {
            "R4": "Off All time",
            "R5": "Off All time",
            "R16": "Off All time",
            "R17": "Off All time",
            "R18": "Off All time",
            "R21": "Off All time",
            "R35": "Off All time",
            "R36": "Off All time",
            "R37": "Off All time",
            "R38": "Off All time"
        },
        "UnSold Occupied": {
            "R4": "Off All time",
            "R5": "Off All time",
            "R16": "On all time",
            "R17": "Off All time",
            "R18": None,
            "R21": None,
            "R35": None,
            "R36": None,
            "R37": None,
            "R38": None
        }
    }
}
