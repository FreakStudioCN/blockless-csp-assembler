import type { BoardDesign, DemoCase, Keepout, Placement, SelectedModule } from "../domain/types";
import { rotatedSize } from "./geometry";

const roleByModule: Record<string, string> = {
  "esp32-devkit-v1": "主控居中，缩短到各接口的平均距离",
  "lora-uart-ra02": "无线模块靠板边，天线朝外并保留keepout",
  "bme280-i2c-board": "传感器放在远离电源的右下角",
  "aht20-i2c-board": "传感器放在远离电源的右下角",
  sht31d: "温湿度传感器靠近外壳通风区域，远离电源热源",
  "sgp30-air-quality": "空气质量传感器靠右侧，给气流留空间",
  "mini-gps-pa1010d": "GNSS 模块靠近板边和顶盖窗口，保留天线视野",
  "ds3231-rtc": "RTC 靠近低速 I2C 总线和日志模块，减少跨板走线",
  "lsm6dsox-lis3mdl": "运动传感器靠近板中心，减少外壳边缘振动影响",
  "tp4056-usb-c-charger": "充电模块靠近左下角和电池入口",
  "jst-ph-2pin": "电池座贴近底边，方便外部接线",
  "qwiic-i2c-connector": "扩展口贴近右边缘",
  "oled-096-i2c": "OLED放上方，作为桌面设备的人机界面",
  "oled-1-5in": "大屏幕靠上方，顶盖窗口直接对准显示区域",
  "usb-c-5v-breakout": "USB-C电源入口贴近左边缘",
  "power-relay-featherwing": "继电器模块靠近外壳侧边，给外部负载端口留开槽空间",
  "featherwing-adalogger": "microSD 记录模块靠近边缘，便于外壳开槽和取卡",
};

function defaultRole(moduleId: string) {
  return roleByModule[moduleId] ?? "按模块类别和板边访问约束自动放置";
}

function clampInitial(module: SelectedModule["module"], placement: Placement, boardSize: { w: number; h: number }): Placement {
  const size = rotatedSize(module, placement);
  return {
    ...placement,
    x: Number(Math.min(boardSize.w - size.w / 2 - 1.8, Math.max(size.w / 2 + 1.8, placement.x)).toFixed(2)),
    y: Number(Math.min(boardSize.h - size.h / 2 - 1.8, Math.max(size.h / 2 + 1.8, placement.y)).toFixed(2)),
  };
}

export function placeModules(demoCase: DemoCase, selectedModules: SelectedModule[]): {
  placements: Placement[];
  keepouts: Keepout[];
} {
  const { w, h } = demoCase.boardSizeMm;
  const categoryCounts = new Map<string, number>();
  const nextCategoryIndex = (category: string) => {
    const index = categoryCounts.get(category) ?? 0;
    categoryCounts.set(category, index + 1);
    return index;
  };

  const sensorSlots = [
    { x: w - 19, y: 17 },
    { x: w - 19, y: h - 17 },
    { x: w * 0.62, y: 17 },
    { x: w * 0.62, y: h - 17 },
  ];
  const connectorSlots = [
    { x: w - 7, y: h * 0.5, rotation: 90 as Placement["rotation"] },
    { x: w - 7, y: h * 0.72, rotation: 90 as Placement["rotation"] },
    { x: w - 7, y: h * 0.28, rotation: 90 as Placement["rotation"] },
  ];

  const placements: Placement[] = selectedModules.map(({ module }) => {
    const categoryIndex = nextCategoryIndex(module.category);
    let placement: Placement;

    if (module.id === "esp32-devkit-v1") {
      placement = { moduleId: module.id, x: demoCase.id === "desktop-air-monitor" ? 18 : 22, y: h * 0.5, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "lora-uart-ra02") {
      placement = { moduleId: module.id, x: w * 0.54, y: h * 0.5, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "tp4056-usb-c-charger") {
      placement = { moduleId: module.id, x: w - 18, y: h - 15, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "jst-ph-2pin") {
      placement = { moduleId: module.id, x: w - 8, y: h - 8, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "bme280-i2c-board" || module.id === "aht20-i2c-board" || module.id === "sht31d" || module.id === "sgp30-air-quality") {
      const slot = sensorSlots[Math.min(categoryIndex, sensorSlots.length - 1)];
      placement = { moduleId: module.id, x: slot.x, y: slot.y, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "qwiic-i2c-connector") {
      const slot = connectorSlots[Math.min(categoryIndex, connectorSlots.length - 1)];
      placement = { moduleId: module.id, ...slot, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "mini-gps-pa1010d") {
      placement = { moduleId: module.id, x: w - 18, y: 18, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "oled-096-i2c" || module.id === "oled-1-5in") {
      placement = { moduleId: module.id, x: w * 0.56, y: 17, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "usb-c-5v-breakout") {
      placement = { moduleId: module.id, x: 11, y: h * 0.5, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "power-relay-featherwing") {
      placement = { moduleId: module.id, x: w - 30, y: h - 20, rotation: 90, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "featherwing-adalogger") {
      placement = { moduleId: module.id, x: w * 0.56, y: h - 17, rotation: 90, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "ds3231-rtc") {
      placement = { moduleId: module.id, x: w * 0.42, y: h - 14, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    if (module.id === "lsm6dsox-lis3mdl") {
      placement = { moduleId: module.id, x: w * 0.52, y: h * 0.52, rotation: 0, role: defaultRole(module.id) };
      return clampInitial(module, placement, demoCase.boardSizeMm);
    }
    placement = { moduleId: module.id, x: w * (0.46 + categoryIndex * 0.08), y: h * (0.45 + categoryIndex * 0.06), rotation: 0, role: defaultRole(module.id) };
    return clampInitial(module, placement, demoCase.boardSizeMm);
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

  const sensor = placements.find((placement) =>
    ["sgp30-air-quality", "bme280-i2c-board", "sht31d", "aht20-i2c-board"].includes(placement.moduleId),
  );
  if (sensor) {
    keepouts.push({
      label: "Sensor airflow keepout",
      x: Math.min(w - 20, sensor.x + 7),
      y: Math.max(4, sensor.y - 8),
      w: 16,
      h: 16,
    });
  }

  const gnss = placements.find((placement) => placement.moduleId === "mini-gps-pa1010d");
  if (gnss) {
    keepouts.push({
      label: "GNSS sky-view keepout",
      x: Math.min(w - 26, gnss.x - 8),
      y: Math.max(4, gnss.y - 16),
      w: 24,
      h: 12,
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
