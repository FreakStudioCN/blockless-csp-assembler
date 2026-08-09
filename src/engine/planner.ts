import { demoCases } from "../data/caseRegistry";
import { getModule, moduleLibrary } from "../data/modules";
import type { DemoCase, SelectedModule } from "../domain/types";

type Locale = "zh" | "en";

function scoreCase(prompt: string, demoCase: DemoCase): number {
  const lower = prompt.toLowerCase();
  const normalized = lower.replace(/[-_\s]/g, "");
  let score = 0;

  if (prompt === demoCase.prompt || prompt === demoCase.promptEn) score += 1000;

  for (const tag of demoCase.intentTags) {
    const normalizedTag = tag.toLowerCase().replace(/[-_\s]/g, "");
    if (lower.includes(tag.toLowerCase()) || normalized.includes(normalizedTag)) score += 2;
  }

  const zhSignals: Record<string, string[]> = {
    "long-range-env-node": ["电池", "长距离", "lora", "环境", "温湿度", "采集"],
    "desktop-air-monitor": ["usb-c", "桌面", "空气质量", "oled", "显示", "监测"],
    "cold-chain-logger": ["冷链", "运输", "记录", "rtc", "microsd", "时间戳"],
    "asset-tracker-node": ["资产", "定位", "跟踪", "gnss", "gps", "lora"],
    "pump-relay-controller": ["泵", "阀", "继电器", "控制", "外壳端口"],
    "motion-diagnostics-panel": ["运动", "诊断", "imu", "磁力计", "microSD", "记录"],
    "greenhouse-sensor-node": ["温室", "农业", "通风", "sht31", "bme280", "传感器"],
    "maintenance-console": ["维护", "控制台", "rtc", "qwiic", "固定孔"],
    "environmental-logger": ["环境", "数据记录", "aht20", "rtc", "microsd"],
    "wireless-gateway": ["gps", "gnss", "rtc", "继电器", "存储", "姿态", "网关"],
    "battery-dock-node": ["电池底座", "便携", "充电", "状态显示", "温湿度"],
    "field-diagnostics-panel": ["现场", "诊断", "继电器", "lora", "固定孔"],
    "maker-core-console": ["存储", "姿态", "运动", "oled", "控制台", "日志"],
  };

  for (const signal of zhSignals[demoCase.id] ?? []) {
    const normalizedSignal = signal.toLowerCase().replace(/[-_\s]/g, "");
    if (lower.includes(signal.toLowerCase()) || normalized.includes(normalizedSignal)) score += 3;
  }

  return score;
}

export function chooseCase(prompt: string): DemoCase {
  return [...demoCases].sort((a, b) => scoreCase(prompt, b) - scoreCase(prompt, a))[0];
}

export function planModules(prompt: string, locale: Locale = "zh"): {
  demoCase: DemoCase;
  selectedModules: SelectedModule[];
} {
  const demoCase = chooseCase(prompt);
  const selectedModules = demoCase.preferredModules.map((id) => {
    const module = getModule(id);
    return {
      module,
      reason: buildReason(module.reasonTags, locale === "en" ? demoCase.nameEn ?? demoCase.name : demoCase.name, locale),
    };
  });

  return { demoCase, selectedModules };
}

function buildReason(tags: string[], caseName: string, locale: Locale): string {
  if (locale === "en") {
    if (tags.includes("rf_black_box")) return "Reuse an off-the-shelf wireless module to avoid designing RF circuitry from scratch.";
    if (tags.includes("charger")) return "Reuse a mature charging module so the board only handles power wiring and mounting.";
    if (tags.includes("gnss")) return "Adds location awareness without building a custom RF front end or antenna chain.";
    if (tags.includes("rtc")) return "Keeps the board time-aware for logging, wake alarms, and offline scheduling.";
    if (tags.includes("relay")) return "Lets the board switch external loads while keeping the control interface simple.";
    if (tags.includes("storage")) return "Provides local logging and removable storage for offline field hardware.";
    if (tags.includes("motion")) return "Supplies motion and heading data for orientation-aware products and enclosures.";
    if (tags.includes("mcu")) return "Use it as the main controller and keep enough I2C, UART, GPIO, and maker-ecosystem support.";
    if (tags.includes("air_quality")) return "It matches the air-quality monitoring use case, and I2C can connect it directly to the main controller.";
    if (tags.includes("display")) return "Provides local display for desktop products while still reusing the I2C bus.";
    if (tags.includes("sensor")) return `Matches the sensing needs of ${caseName} and keeps the board simple through standard interfaces.`;
    if (tags.includes("connector")) return "Works as an external expansion or power entry point, which fits small-batch maker hardware.";
    return "Pulled from the reviewed module library and fits the current board generation flow.";
  }

  if (tags.includes("rf_black_box")) return "复用现成无线模块，避免从零设计射频电路。";
  if (tags.includes("charger")) return "复用成熟充电模块，底板只处理供电连接和安装位置。";
  if (tags.includes("gnss")) return "加入定位能力，避免从头做天线和射频链路。";
  if (tags.includes("rtc")) return "让底板具备时间感知，适合记录、定时唤醒和离线调度。";
  if (tags.includes("relay")) return "可以直接控制外部负载或电源，适合小型控制面板。";
  if (tags.includes("storage")) return "为本地日志提供可移动存储，无网络环境也能用。";
  if (tags.includes("motion")) return "为注重姿态和方向的产品提供运动数据。";
  if (tags.includes("mcu")) return "作为主控模块，提供足够的 I2C、UART、GPIO 和创客生态支持。";
  if (tags.includes("air_quality")) return "匹配空气质量监测需求，I2C 接口可以直接挂接主控。";
  if (tags.includes("display")) return "为桌面类产品提供本地显示，同时继续复用 I2C 总线。";
  if (tags.includes("sensor")) return `匹配 ${caseName} 的传感需求，并通过标准接口降低底板复杂度。`;
  if (tags.includes("connector")) return "作为外部扩展或电源入口，适合小批量创客硬件。";
  return "来自已审核的模块库，适合当前底板生成流程。";
}

export function getAlternativeModules(category: string) {
  return moduleLibrary.filter((item) => item.category === category);
}
