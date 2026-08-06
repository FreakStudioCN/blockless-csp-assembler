import type { BoardDesign, DemoCase, Keepout, Placement, SelectedModule } from "../domain/types";

const roleByModule: Record<string, string> = {
  "esp32-devkit-v1": "主控居中，缩短到各接口的平均距离",
  "lora-uart-ra02": "无线模块靠板边，天线朝外并保留keepout",
  "bme280-i2c-board": "传感器放在远离电源的右下角",
  "aht20-i2c-board": "传感器放在远离电源的右下角",
  "tp4056-usb-c-charger": "充电模块靠近左下角和电池入口",
  "jst-ph-2pin": "电池座贴近底边，方便外部接线",
  "qwiic-i2c-connector": "扩展口贴近右边缘",
  "sgp30-air-quality": "空气质量传感器靠右侧，给气流留空间",
  "oled-096-i2c": "OLED放上方，作为桌面设备的人机界面",
  "usb-c-5v-breakout": "USB-C电源入口贴近左边缘",
};

export function placeModules(demoCase: DemoCase, selectedModules: SelectedModule[]): {
  placements: Placement[];
  keepouts: Keepout[];
} {
  const { w, h } = demoCase.boardSizeMm;
  const placements: Placement[] = selectedModules.map(({ module }) => {
    if (module.id === "esp32-devkit-v1") {
      return { moduleId: module.id, x: demoCase.id === "desktop-air-monitor" ? 18 : 22, y: demoCase.id === "desktop-air-monitor" ? 32 : 34, rotation: 0, role: roleByModule[module.id] };
    }
    if (module.id === "lora-uart-ra02") {
      return { moduleId: module.id, x: 55, y: 34, rotation: 0, role: roleByModule[module.id] };
    }
    if (module.id === "tp4056-usb-c-charger") {
      return { moduleId: module.id, x: 78, y: 52, rotation: 0, role: roleByModule[module.id] };
    }
    if (module.id === "jst-ph-2pin") {
      return { moduleId: module.id, x: 92, y: 61, rotation: 0, role: roleByModule[module.id] };
    }
    if (module.id === "bme280-i2c-board" || module.id === "aht20-i2c-board") {
      return { moduleId: module.id, x: 78, y: 20, rotation: 0, role: roleByModule[module.id] };
    }
    if (module.id === "qwiic-i2c-connector") {
      return {
        moduleId: module.id,
        x: demoCase.id === "desktop-air-monitor" ? 84 : 93,
        y: demoCase.id === "desktop-air-monitor" ? 34 : 34,
        rotation: 90,
        role: roleByModule[module.id],
      };
    }
    if (module.id === "sgp30-air-quality") {
      return { moduleId: module.id, x: 58, y: 47, rotation: 0, role: roleByModule[module.id] };
    }
    if (module.id === "oled-096-i2c") {
      return { moduleId: module.id, x: 58, y: 18, rotation: 0, role: roleByModule[module.id] };
    }
    if (module.id === "usb-c-5v-breakout") {
      return { moduleId: module.id, x: 76, y: 54, rotation: 0, role: roleByModule[module.id] };
    }
    return { moduleId: module.id, x: w * 0.5, y: h * 0.5, rotation: 0, role: "按默认网格放置" };
  });

  const keepouts: Keepout[] = [];
  const lora = placements.find((placement) => placement.moduleId === "lora-uart-ra02");
  if (lora) {
    keepouts.push({
      label: "LoRa antenna keepout",
      x: Math.min(w - 30, lora.x + 13),
      y: Math.max(4, lora.y - 30),
      w: 28,
      h: 8,
    });
  }

  const sensor = placements.find((placement) => placement.moduleId === "sgp30-air-quality");
  if (sensor) {
    keepouts.push({
      label: "Airflow keepout",
      x: Math.min(w - 20, sensor.x + 7),
      y: Math.max(4, sensor.y - 8),
      w: 16,
      h: 16,
    });
  }

  return { placements, keepouts };
}

export function getPlacement(design: Pick<BoardDesign, "placements">, moduleId: string): Placement {
  const placement = design.placements.find((item) => item.moduleId === moduleId);
  if (!placement) {
    throw new Error(`Placement not found: ${moduleId}`);
  }
  return placement;
}
