import type { NetConnection, SelectedModule } from "../domain/types";

function hasModule(selected: SelectedModule[], id: string): boolean {
  return selected.some((item) => item.module.id === id);
}

function endpoint(moduleId: string, pin: string) {
  return { moduleId, pin };
}

function powerInputPin(selected: SelectedModule[], moduleId: string, rail: string) {
  const item = selected.find((candidate) => candidate.module.id === moduleId);
  return item?.module.pins.find((pin) => pin.type === "power_in" && pin.net === rail);
}

export function matchInterfaces(selectedModules: SelectedModule[]): NetConnection[] {
  const selected = selectedModules;
  const nets: NetConnection[] = [];
  const mcuId = "esp32-devkit-v1";
  const powerSource = hasModule(selected, "tp4056-usb-c-charger")
    ? "tp4056-usb-c-charger"
    : "usb-c-5v-breakout";

  nets.push({
    net: "5V",
    kind: "power",
    from: endpoint(powerSource, powerSource === "tp4056-usb-c-charger" ? "OUT+" : "VBUS"),
    to: [endpoint(mcuId, "VIN")],
    widthMm: 0.6,
    layer: "top",
  });

  const fiveVoltTargets = selected
    .filter((item) => item.module.id !== mcuId && item.module.id !== powerSource)
    .map((item) => {
      const pin = powerInputPin(selected, item.module.id, "5V");
      return pin ? endpoint(item.module.id, pin.name) : null;
    })
    .filter((item): item is { moduleId: string; pin: string } => Boolean(item));

  if (fiveVoltTargets.length > 0) {
    nets.push({
      net: "5V_MODULES",
      kind: "power",
      from: endpoint(powerSource, powerSource === "tp4056-usb-c-charger" ? "OUT+" : "VBUS"),
      to: fiveVoltTargets,
      widthMm: 0.55,
      layer: "top",
    });
  }

  if (hasModule(selected, "tp4056-usb-c-charger")) {
    nets.push({
      net: "BAT+",
      kind: "power",
      from: endpoint("jst-ph-2pin", "BAT+"),
      to: [endpoint("tp4056-usb-c-charger", "B+")],
      widthMm: 0.8,
      layer: "top",
    });
  }

  const gndTargets = selected
    .map((item) => {
      const pin = item.module.pins.find((candidate) => candidate.type === "gnd");
      return pin ? endpoint(item.module.id, pin.name) : null;
    })
    .filter((item): item is { moduleId: string; pin: string } => Boolean(item));

  nets.push({
    net: "GND",
    kind: "gnd",
    from: gndTargets[0],
    to: gndTargets.slice(1),
    widthMm: 0.7,
    layer: "bottom",
  });

  const threeVoltTargets = selected
    .filter((item) => item.module.id !== mcuId)
    .map((item) => {
      const pin = powerInputPin(selected, item.module.id, "3V3");
      return pin ? endpoint(item.module.id, pin.name) : null;
    })
    .filter((item): item is { moduleId: string; pin: string } => Boolean(item));

  if (threeVoltTargets.length > 0) {
    nets.push({
      net: "3V3",
      kind: "power",
      from: endpoint(mcuId, "3V3"),
      to: threeVoltTargets,
      widthMm: 0.45,
      layer: "top",
    });
  }

  const i2cTargets = selected
    .filter((item) => item.module.id !== mcuId && item.module.interfaces.includes("i2c"))
    .map((item) => item.module.id);

  if (i2cTargets.length > 0) {
    nets.push(
      {
        net: "I2C_SDA",
        kind: "i2c",
        from: endpoint(mcuId, "GPIO21"),
        to: i2cTargets.map((moduleId) => endpoint(moduleId, "SDA")),
        widthMm: 0.25,
        layer: "top",
      },
      {
        net: "I2C_SCL",
        kind: "i2c",
        from: endpoint(mcuId, "GPIO22"),
        to: i2cTargets.map((moduleId) => endpoint(moduleId, "SCL")),
        widthMm: 0.25,
        layer: "top",
      },
    );
  }

  if (hasModule(selected, "mini-gps-pa1010d")) {
    nets.push(
      {
        net: "GPS_TX",
        kind: "uart",
        from: endpoint("mini-gps-pa1010d", "TX"),
        to: [endpoint(mcuId, "GPIO25")],
        widthMm: 0.25,
        layer: "bottom",
      },
      {
        net: "GPS_RX",
        kind: "uart",
        from: endpoint(mcuId, "GPIO26"),
        to: [endpoint("mini-gps-pa1010d", "RX")],
        widthMm: 0.25,
        layer: "bottom",
      },
      {
        net: "GPS_PPS",
        kind: "gpio",
        from: endpoint("mini-gps-pa1010d", "PPS"),
        to: [endpoint(mcuId, "GPIO27")],
        widthMm: 0.2,
        layer: "top",
      },
    );
  }

  if (hasModule(selected, "featherwing-adalogger")) {
    nets.push(
      {
        net: "SD_SCK",
        kind: "spi",
        from: endpoint(mcuId, "GPIO18"),
        to: [endpoint("featherwing-adalogger", "SCK")],
        widthMm: 0.25,
        layer: "top",
      },
      {
        net: "SD_MOSI",
        kind: "spi",
        from: endpoint(mcuId, "GPIO23"),
        to: [endpoint("featherwing-adalogger", "MOSI")],
        widthMm: 0.25,
        layer: "top",
      },
      {
        net: "SD_MISO",
        kind: "spi",
        from: endpoint("featherwing-adalogger", "MISO"),
        to: [endpoint(mcuId, "GPIO19")],
        widthMm: 0.25,
        layer: "bottom",
      },
      {
        net: "SD_CS",
        kind: "spi",
        from: endpoint(mcuId, "GPIO15"),
        to: [endpoint("featherwing-adalogger", "CS")],
        widthMm: 0.25,
        layer: "bottom",
      },
    );
  }

  if (hasModule(selected, "power-relay-featherwing")) {
    nets.push({
      net: "RELAY_IN",
      kind: "gpio",
      from: endpoint(mcuId, "GPIO32"),
      to: [endpoint("power-relay-featherwing", "IN")],
      widthMm: 0.25,
      layer: "top",
    });
  }

  if (hasModule(selected, "lora-uart-ra02")) {
    nets.push(
      {
        net: "LORA_TX",
        kind: "uart",
        from: endpoint("lora-uart-ra02", "TX"),
        to: [endpoint(mcuId, "GPIO16")],
        widthMm: 0.25,
        layer: "top",
      },
      {
        net: "LORA_RX",
        kind: "uart",
        from: endpoint(mcuId, "GPIO17"),
        to: [endpoint("lora-uart-ra02", "RX")],
        widthMm: 0.25,
        layer: "bottom",
      },
    );
  }

  return nets;
}
