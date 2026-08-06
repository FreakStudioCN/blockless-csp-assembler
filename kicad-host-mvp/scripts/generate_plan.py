import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "plans"
PRESENTATION_CAD = ROOT / "assets" / "presentation-cad"


def write_plan(name, payload):
    PLANS.mkdir(parents=True, exist_ok=True)
    path = PLANS / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path.name)


def hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[index : index + 2], 16) / 255 for index in (0, 2, 4))


def wrl_material(name, color, specular=(0.12, 0.12, 0.12), shininess=0.35):
    r, g, b = hex_to_rgb(color)
    sr, sg, sb = specular
    return f"""DEF {name} Appearance {{
  material Material {{
    diffuseColor {r:.4f} {g:.4f} {b:.4f}
    specularColor {sr:.4f} {sg:.4f} {sb:.4f}
    ambientIntensity 0.18
    shininess {shininess:.4f}
  }}
}}"""


def wrl_box(name, material, x, y, z, w, h, d):
    x1, x2 = x - w / 2, x + w / 2
    y1, y2 = y - h / 2, y + h / 2
    z1, z2 = z, z + d
    points = [
        (x1, y1, z1),
        (x2, y1, z1),
        (x2, y2, z1),
        (x1, y2, z1),
        (x1, y1, z2),
        (x2, y1, z2),
        (x2, y2, z2),
        (x1, y2, z2),
    ]
    point_text = ",\n          ".join(f"{px:.4f} {py:.4f} {pz:.4f}" for px, py, pz in points)
    return f"""DEF {name} Transform {{
  children [
    Shape {{
      appearance USE {material}
      geometry IndexedFaceSet {{
        coord Coordinate {{ point [
          {point_text}
        ] }}
        coordIndex [
          0, 3, 2, 1, -1,
          4, 5, 6, 7, -1,
          0, 4, 5, 1, -1,
          1, 5, 6, 2, -1,
          2, 6, 7, 3, -1,
          3, 7, 4, 0, -1
        ]
      }}
    }}
  ]
}}"""


def write_presentation_model(module_id, width, height, board_color, detail_style):
    PRESENTATION_CAD.mkdir(parents=True, exist_ok=True)
    materials = [
        wrl_material("board", board_color, specular=(0.05, 0.05, 0.05), shininess=0.22),
        wrl_material("gold", "#d7b35a", specular=(0.35, 0.30, 0.18), shininess=0.55),
        wrl_material("black", "#15171c", specular=(0.10, 0.10, 0.10), shininess=0.20),
        wrl_material("silver", "#c9d0d3", specular=(0.45, 0.45, 0.45), shininess=0.62),
        wrl_material("white", "#f1f2e8", specular=(0.15, 0.15, 0.12), shininess=0.28),
        wrl_material("glass", "#07111f", specular=(0.30, 0.36, 0.48), shininess=0.80),
    ]
    boxes = [wrl_box("pcb", "board", 0, 0, 0, width, height, 0.9)]

    pin_count = 12 if height > 20 else 4
    for index in range(pin_count):
        py = -height * 0.42 + index * height * 0.84 / max(1, pin_count - 1)
        for px in (-width * 0.42, width * 0.42):
            boxes.append(wrl_box(f"pad_{index}_{px > 0}", "gold", px, py, 0.91, 1.15, 1.15, 0.08))

    if detail_style == "esp32":
        boxes.extend(
            [
                wrl_box("esp_module", "silver", 0, -height * 0.08, 1.0, width * 0.58, height * 0.28, 1.25),
                wrl_box("antenna", "black", 0, height * 0.34, 1.04, width * 0.72, height * 0.18, 0.22),
                wrl_box("usb", "silver", 0, -height * 0.48, 1.0, width * 0.42, 3.8, 1.2),
                wrl_box("chip", "black", -width * 0.18, height * 0.12, 1.0, width * 0.22, height * 0.14, 0.72),
            ]
        )
    elif detail_style == "radio":
        boxes.extend(
            [
                wrl_box("rf_shield", "silver", 0, -height * 0.02, 1.0, width * 0.48, height * 0.30, 1.18),
                wrl_box("rf_chip", "black", -width * 0.16, height * 0.22, 1.0, width * 0.24, height * 0.16, 0.75),
                wrl_box("antenna_area", "gold", 0, height * 0.42, 1.0, width * 0.68, height * 0.10, 0.12),
            ]
        )
    elif detail_style == "sensor":
        boxes.extend(
            [
                wrl_box("sensor_ic", "black", 0, 0, 1.0, width * 0.35, height * 0.35, 0.62),
                wrl_box("cap_a", "white", -width * 0.26, height * 0.22, 1.0, 1.5, 0.9, 0.32),
                wrl_box("cap_b", "gold", width * 0.26, -height * 0.22, 1.0, 1.5, 0.9, 0.32),
            ]
        )
    elif detail_style == "power":
        boxes.extend(
            [
                wrl_box("usb", "silver", 0, -height * 0.38, 1.0, width * 0.62, height * 0.18, 1.1),
                wrl_box("jst", "black", 0, height * 0.30, 1.0, width * 0.48, height * 0.22, 1.05),
                wrl_box("charger_ic", "black", -width * 0.20, 0, 1.0, width * 0.25, height * 0.18, 0.55),
                wrl_box("led", "white", width * 0.30, -height * 0.08, 1.0, 1.1, 0.8, 0.28),
            ]
        )
    elif detail_style == "connector":
        boxes.append(wrl_box("connector_body", "white", 0, 0, 1.0, width * 0.72, height * 0.62, 1.25))
    elif detail_style == "display":
        boxes.extend(
            [
                wrl_box("glass", "glass", 0, 0, 1.0, width * 0.75, height * 0.48, 0.42),
                wrl_box("connector", "gold", 0, -height * 0.42, 1.0, width * 0.50, height * 0.08, 0.32),
            ]
        )

    body = "\n\n".join(materials + boxes)
    path = PRESENTATION_CAD / f"{module_id}.wrl"
    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(f"#VRML V2.0 utf8\n\n{body}\n")
    return f"../assets/presentation-cad/{module_id}.wrl"


presentation_models = {
    "esp32-feather": write_presentation_model("esp32-feather", 23, 51, "#245f73", "esp32"),
    "radio-featherwing": write_presentation_model("radio-featherwing", 23, 51, "#c94e3d", "radio"),
    "bme280": write_presentation_model("bme280", 16, 12, "#1c73d8", "sensor"),
    "microlipo": write_presentation_model("microlipo", 16, 25, "#8a57d6", "power"),
    "jst-ph-2pin": write_presentation_model("jst-ph-2pin", 8, 7, "#f5f1df", "connector"),
    "i2c-expansion": write_presentation_model("i2c-expansion", 12, 8, "#e9ad22", "connector"),
    "oled-128x32": write_presentation_model("oled-128x32", 27, 27, "#264f9f", "display"),
    "usb-c-5v-breakout": write_presentation_model("usb-c-5v-breakout", 14, 18, "#b68a3a", "connector"),
}


base_modules = {
    "esp32": {
        "id": "esp32-feather",
        "name": "Adafruit HUZZAH32 ESP32 Feather",
        "size_mm": [23, 51],
        "pins": ["VIN", "3V3", "GND", "SDA", "SCL", "TX", "RX"],
        "cad": {
            "presentation": presentation_models["esp32-feather"],
            "stl": "../../public/assets/real-cad/esp32-feather.stl",
            "step": "../../public/assets/real-cad/esp32-feather.step",
        },
    },
    "radio": {
        "id": "radio-featherwing",
        "name": "Adafruit Radio FeatherWing RFM95",
        "size_mm": [23, 51],
        "pins": ["VCC", "GND", "TX", "RX"],
        "cad": {
            "presentation": presentation_models["radio-featherwing"],
            "stl": "../../public/assets/real-cad/radio-featherwing.stl",
            "step": "../../public/assets/real-cad/radio-featherwing.step",
        },
    },
    "bme280": {
        "id": "bme280",
        "name": "Adafruit BME280 I2C",
        "size_mm": [16, 12],
        "pins": ["VCC", "GND", "SDA", "SCL"],
        "cad": {
            "presentation": presentation_models["bme280"],
            "stl": "../../public/assets/real-cad/bme280.stl",
            "step": "../../public/assets/real-cad/bme280.step",
        },
    },
    "charger": {
        "id": "microlipo",
        "name": "Adafruit MicroLipo Charger",
        "size_mm": [16, 25],
        "pins": ["OUT+", "OUT-", "B+", "B-"],
        "cad": {
            "presentation": presentation_models["microlipo"],
            "stl": "../../public/assets/real-cad/microlipo.stl",
            "step": "../../public/assets/real-cad/microlipo.step",
        },
    },
    "i2c": {
        "id": "i2c-expansion",
        "name": "Qwiic/Grove I2C Expansion",
        "size_mm": [12, 8],
        "pins": ["VCC", "GND", "SDA", "SCL"],
        "cad": {
            "presentation": presentation_models["i2c-expansion"],
        },
    },
    "jst": {
        "id": "jst-ph-2pin",
        "name": "JST-PH 2Pin Battery Connector",
        "size_mm": [8, 7],
        "pins": ["BAT+", "GND"],
        "cad": {
            "presentation": presentation_models["jst-ph-2pin"],
        },
    },
    "oled": {
        "id": "oled-128x32",
        "name": "OLED I2C Display",
        "size_mm": [27, 27],
        "pins": ["VCC", "GND", "SDA", "SCL"],
        "cad": {
            "presentation": presentation_models["oled-128x32"],
            "step": "../../public/assets/real-cad/oled-128x32.step",
        },
    },
    "usb_c": {
        "id": "usb-c-5v-breakout",
        "name": "USB-C 5V Power Breakout",
        "size_mm": [14, 18],
        "pins": ["VBUS", "GND"],
        "cad": {
            "presentation": presentation_models["usb-c-5v-breakout"],
        },
    },
}


write_plan(
    "long-range-env-node",
    {
        "title": "Long-range environment node",
        "board_mm": [100, 68],
        "modules": [
            {**base_modules["esp32"], "placement": [22, 34, 0]},
            {**base_modules["radio"], "placement": [55, 34, 0], "keepout": [68, 4, 28, 8]},
            {**base_modules["bme280"], "placement": [78, 20, 0]},
            {**base_modules["charger"], "placement": [78, 52, 0]},
            {**base_modules["jst"], "placement": [92, 61, 0]},
            {**base_modules["i2c"], "placement": [93, 34, 90]},
        ],
        "nets": [
            ["BAT+", "jst-ph-2pin.BAT+", ["microlipo.B+"]],
            ["5V", "microlipo.OUT+", ["esp32-feather.VIN", "radio-featherwing.VCC"]],
            ["3V3", "esp32-feather.3V3", ["bme280.VCC", "i2c-expansion.VCC"]],
            [
                "GND",
                "microlipo.OUT-",
                ["esp32-feather.GND", "radio-featherwing.GND", "bme280.GND", "i2c-expansion.GND", "jst-ph-2pin.GND", "microlipo.B-"],
            ],
            ["I2C_SDA", "esp32-feather.SDA", ["bme280.SDA", "i2c-expansion.SDA"]],
            ["I2C_SCL", "esp32-feather.SCL", ["bme280.SCL", "i2c-expansion.SCL"]],
            ["LORA_TX", "radio-featherwing.TX", ["esp32-feather.RX"]],
            ["LORA_RX", "esp32-feather.TX", ["radio-featherwing.RX"]],
        ],
    },
)

write_plan(
    "desktop-air-monitor",
    {
        "title": "Desktop air quality monitor",
        "board_mm": [92, 64],
        "modules": [
            {**base_modules["esp32"], "placement": [18, 32, 0]},
            {**base_modules["bme280"], "name": "SGP30/Air Quality I2C Module", "placement": [58, 47, 0]},
            {**base_modules["oled"], "placement": [58, 18, 0]},
            {**base_modules["usb_c"], "placement": [76, 54, 0]},
            {**base_modules["i2c"], "placement": [84, 34, 90]},
        ],
        "nets": [
            ["5V", "usb-c-5v-breakout.VBUS", ["esp32-feather.VIN"]],
            ["3V3", "esp32-feather.3V3", ["bme280.VCC", "oled-128x32.VCC", "i2c-expansion.VCC"]],
            ["GND", "esp32-feather.GND", ["bme280.GND", "oled-128x32.GND", "i2c-expansion.GND", "usb-c-5v-breakout.GND"]],
            ["I2C_SDA", "esp32-feather.SDA", ["bme280.SDA", "oled-128x32.SDA", "i2c-expansion.SDA"]],
            ["I2C_SCL", "esp32-feather.SCL", ["bme280.SCL", "oled-128x32.SCL", "i2c-expansion.SCL"]],
        ],
    },
)
