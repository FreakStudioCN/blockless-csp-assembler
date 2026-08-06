export interface RetailLink {
  label: string;
  url: string;
  imageUrl: string;
  hint?: string;
}

export interface ModuleMarketplace {
  amazon: RetailLink;
  lcsc: RetailLink;
}

export const moduleMarketplace: Record<string, ModuleMarketplace> = {
  "esp32-devkit-v1": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/dp/B079QGXX88",
      imageUrl: "/assets/marketplace/amazon/esp32-feather.jpg",
      hint: "ESP32 Feather",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=esp32%20devkit",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "lora-uart-ra02": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/dp/B01LHU153C",
      imageUrl: "/assets/marketplace/amazon/lora-featherwing.jpg",
      hint: "LoRa FeatherWing",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=lora%20uart%20module",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "bme280-i2c-board": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/dp/B07V5CL3L8",
      imageUrl: "/assets/marketplace/amazon/bme280.jpg",
      hint: "BME280 module",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=bme280%20module",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "aht20-i2c-board": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/s?k=AHT20+sensor+module",
      imageUrl: "",
      hint: "AHT20 module",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=aht20%20module",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "tp4056-usb-c-charger": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/dp/B00OKJFEQ2",
      imageUrl: "/assets/marketplace/amazon/microlipo.jpg",
      hint: "MicroLipo charger",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=tp4056%20usb-c%20module",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "jst-ph-2pin": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/CQRobot-Pieces-Connector-Housing-Adapter/dp/B09DP9FZTX/ref=sr_1_1",
      imageUrl: "/assets/marketplace/amazon/jst-ph-2pin.jpg",
      hint: "JST PH housing",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=jst%20ph%202pin",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "qwiic-i2c-connector": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/elechawk-SparkFun-Development-Breadboard-Connector/dp/B08HQ1VSVL/ref=sr_1_1",
      imageUrl: "/assets/marketplace/amazon/qwiic-connector.jpg",
      hint: "Qwiic / SH 4-pin",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=qwiic%20connector",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "sgp30-air-quality": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/s?k=SGP30+module",
      imageUrl: "",
      hint: "SGP30 module",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=sgp30%20module",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "oled-096-i2c": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/Hosyond-Display-Self-Luminous-Compatible-Raspberry/dp/B09T6SJBV5/ref=sr_1_1",
      imageUrl: "/assets/marketplace/amazon/oled-096.jpg",
      hint: "0.96 OLED",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=0.96%20oled%20i2c",
      imageUrl: "",
      hint: "备选采购",
    },
  },
  "usb-c-5v-breakout": {
    amazon: {
      label: "Amazon",
      url: "https://www.amazon.com/Cermant-Breakout-Serial-Connector-Converter/dp/B0CB2VFJ54/ref=sr_1_1",
      imageUrl: "/assets/marketplace/amazon/usb-c-breakout.jpg",
      hint: "USB-C breakout",
    },
    lcsc: {
      label: "LCSC",
      url: "https://www.lcsc.com/search?q=usb-c%205v%20breakout",
      imageUrl: "",
      hint: "备选采购",
    },
  },
};
