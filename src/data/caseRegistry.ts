import type { DemoCase } from "../domain/types";

export const demoCases: DemoCase[] = [
  {
    id: "long-range-env-node",
    name: "远距离环境采集节点",
    prompt:
      "做一个电池供电的远距离环境采集节点，支持ESP32、LoRa、温湿度传感器、锂电池充电和I2C扩展。",
    intentTags: ["battery", "long_range", "environment", "i2c", "lora"],
    preferredModules: [
      "esp32-devkit-v1",
      "lora-uart-ra02",
      "bme280-i2c-board",
      "tp4056-usb-c-charger",
      "jst-ph-2pin",
      "qwiic-i2c-connector",
    ],
    boardSizeMm: { w: 100, h: 68 },
  },
  {
    id: "desktop-air-monitor",
    name: "桌面空气质量监测器",
    prompt:
      "做一个USB-C供电的桌面空气质量监测器，支持ESP32、空气质量传感器、OLED显示和I2C扩展。",
    intentTags: ["desktop", "air_quality", "display", "i2c", "usb_c"],
    preferredModules: [
      "esp32-devkit-v1",
      "sgp30-air-quality",
      "oled-096-i2c",
      "usb-c-5v-breakout",
      "qwiic-i2c-connector",
    ],
    boardSizeMm: { w: 92, h: 64 },
  },
];

export const defaultPrompt = demoCases[0].prompt;
