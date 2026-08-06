import { demoCases } from "../data/caseRegistry";
import { getModule, moduleLibrary } from "../data/modules";
import type { DemoCase, SelectedModule } from "../domain/types";

function scoreCase(prompt: string, demoCase: DemoCase): number {
  const lower = prompt.toLowerCase();
  let score = 0;

  for (const tag of demoCase.intentTags) {
    if (lower.includes(tag.toLowerCase())) score += 2;
  }

  const zhSignals: Record<string, string[]> = {
    "long-range-env-node": ["电池", "远距离", "lora", "环境", "温湿度", "采集"],
    "desktop-air-monitor": ["usb-c", "桌面", "空气质量", "oled", "显示", "监测"],
  };

  for (const signal of zhSignals[demoCase.id] ?? []) {
    if (lower.includes(signal.toLowerCase())) score += 3;
  }

  return score;
}

export function chooseCase(prompt: string): DemoCase {
  return [...demoCases].sort((a, b) => scoreCase(prompt, b) - scoreCase(prompt, a))[0];
}

export function planModules(prompt: string): {
  demoCase: DemoCase;
  selectedModules: SelectedModule[];
} {
  const demoCase = chooseCase(prompt);
  const selectedModules = demoCase.preferredModules.map((id) => {
    const module = getModule(id);
    return {
      module,
      reason: buildReason(module.reasonTags, demoCase.name),
    };
  });

  return { demoCase, selectedModules };
}

function buildReason(tags: string[], caseName: string): string {
  if (tags.includes("rf_black_box")) {
    return "复用现成无线模块，避免从零设计射频电路。";
  }
  if (tags.includes("charger")) {
    return "复用成熟充电模块，底板只处理电源连接和安装位置。";
  }
  if (tags.includes("mcu")) {
    return "作为主控模块，提供足够的I2C、UART、GPIO和创客生态支持。";
  }
  if (tags.includes("air_quality")) {
    return "匹配空气质量监测需求，I2C接口能直接挂接主控。";
  }
  if (tags.includes("display")) {
    return "为桌面类产品提供本地显示，继续复用I2C总线。";
  }
  if (tags.includes("sensor")) {
    return `匹配${caseName}的传感需求，并通过标准接口降低底板复杂度。`;
  }
  if (tags.includes("connector")) {
    return "作为外部扩展或电源入口，适合小批量创客硬件。";
  }
  return "来自已审阅模块库，适合当前底板生成流程。";
}

export function getAlternativeModules(category: string) {
  return moduleLibrary.filter((item) => item.category === category);
}
