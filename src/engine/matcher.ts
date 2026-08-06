import type { NetConnection, SelectedModule } from "../domain/types";

function hasModule(selected: SelectedModule[], id: string): boolean {
  return selected.some((item) => item.module.id === id);
}

function endpoint(moduleId: string, pin: string) {
  return { moduleId, pin };
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

  const i2cTargets = selected
    .filter((item) => item.module.id !== mcuId && item.module.interfaces.includes("i2c"))
    .map((item) => item.module.id);

  if (i2cTargets.length > 0) {
    nets.push(
      {
        net: "3V3",
        kind: "power",
        from: endpoint(mcuId, "3V3"),
        to: i2cTargets.map((moduleId) =>
          endpoint(moduleId, moduleId === "sgp30-air-quality" ? "VIN" : "VCC"),
        ),
        widthMm: 0.45,
        layer: "top",
      },
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
