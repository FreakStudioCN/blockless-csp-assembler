import type { BoardDesign, BomLine, CheckItem, CheckReport, HardwareModule, Placement, SelectedModule } from "../domain/types";

type Locale = "zh" | "en";

type Rect = {
  moduleId: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
};

export function buildBom(selectedModules: SelectedModule[]): BomLine[] {
  return selectedModules.map(({ module }, index) => ({
    item: index + 1,
    moduleId: module.id,
    name: module.name,
    qty: 1,
    connector: module.connector,
    source: module.source,
    purchaseUrl: module.purchaseUrl,
    unitPriceUsd: module.unitPriceUsd,
  }));
}

function moduleMap(selectedModules: SelectedModule[]) {
  return new Map(selectedModules.map(({ module }) => [module.id, module]));
}

function placementRect(placement: Placement, module: HardwareModule): Rect {
  const rotated = placement.rotation === 90 || placement.rotation === 270;
  const width = rotated ? module.sizeMm.h : module.sizeMm.w;
  const height = rotated ? module.sizeMm.w : module.sizeMm.h;
  return {
    moduleId: module.id,
    x1: placement.x - width / 2,
    y1: placement.y - height / 2,
    x2: placement.x + width / 2,
    y2: placement.y + height / 2,
  };
}

function rectGap(a: Rect, b: Rect) {
  const dx = Math.max(0, Math.max(a.x1 - b.x2, b.x1 - a.x2));
  const dy = Math.max(0, Math.max(a.y1 - b.y2, b.y1 - a.y2));
  if (dx > 0 && dy > 0) return Math.hypot(dx, dy);
  if (dx > 0) return dx;
  if (dy > 0) return dy;
  const overlapX = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1);
  const overlapY = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1);
  return -Math.min(overlapX, overlapY);
}

function moduleMechanicalChecks(design: Omit<BoardDesign, "report" | "bom">, locale: Locale): CheckItem[] {
  const modules = moduleMap(design.selectedModules);
  const rects = design.placements.flatMap((placement) => {
    const module = modules.get(placement.moduleId);
    return module ? [placementRect(placement, module)] : [];
  });
  const checks: CheckItem[] = [];
  const edgeMargin = Math.min(
    ...rects.map((rect) => Math.min(rect.x1, rect.y1, design.boardSizeMm.w - rect.x2, design.boardSizeMm.h - rect.y2)),
  );

  if (edgeMargin < 0) {
    checks.push({
      level: "fail",
      code: "DRC-MECH-EDGE",
      message:
        locale === "en"
          ? `Module outline exceeds the board edge by ${Math.abs(edgeMargin).toFixed(1)} mm.`
          : `模块外形超出板框 ${Math.abs(edgeMargin).toFixed(1)} mm。`,
    });
  } else {
    checks.push({
      level: "pass",
      code: "DRC-MECH-EDGE",
      message:
        locale === "en"
          ? `All module outlines are at least ${edgeMargin.toFixed(1)} mm from the board edge.`
          : `所有模块外形距离板边至少 ${edgeMargin.toFixed(1)} mm。`,
    });
  }

  let minGap = Number.POSITIVE_INFINITY;
  let closestPair = "";
  for (let index = 0; index < rects.length; index += 1) {
    for (let otherIndex = index + 1; otherIndex < rects.length; otherIndex += 1) {
      const gap = rectGap(rects[index], rects[otherIndex]);
      if (gap < minGap) {
        minGap = gap;
        closestPair = `${rects[index].moduleId} / ${rects[otherIndex].moduleId}`;
      }
    }
  }

  if (minGap < 0) {
    checks.push({
      level: "fail",
      code: "DRC-MECH-OVERLAP",
      message:
        locale === "en"
          ? `Mechanical module keepouts overlap by ${Math.abs(minGap).toFixed(1)} mm: ${closestPair}.`
          : `模块机械占位重叠 ${Math.abs(minGap).toFixed(1)} mm：${closestPair}。`,
    });
  } else {
    checks.push({
      level: "pass",
      code: "DRC-MECH-CLEAR",
      message:
        locale === "en"
          ? `No mechanical overlap; closest clearance is ${minGap.toFixed(1)} mm: ${closestPair}.`
          : `模块机械占位无重叠，最近间距 ${minGap.toFixed(1)} mm：${closestPair}。`,
    });
  }

  return checks;
}

export function checkDesign(design: Omit<BoardDesign, "report" | "bom">, locale: Locale = "zh"): CheckReport {
  const selectedWithGround = design.selectedModules
    .filter(({ module }) => module.pins.some((pin) => pin.type === "gnd"))
    .map(({ module }) => module.id);
  const groundNet = design.nets.find((net) => net.kind === "gnd");
  const groundedModules = new Set(groundNet ? [groundNet.from.moduleId, ...groundNet.to.map((endpoint) => endpoint.moduleId)] : []);
  const groundIntentComplete = selectedWithGround.every((moduleId) => groundedModules.has(moduleId));

  const erc: CheckItem[] = [
    {
      level: groundIntentComplete ? "pass" : "fail",
      code: "INTENT-GND-001",
      message:
        locale === "en"
          ? groundIntentComplete
            ? `The browser netlist assigns GND endpoints to all ${selectedWithGround.length} grounded modules; this is not an electrical connectivity test.`
            : "The browser netlist is missing one or more expected GND endpoints."
          : groundIntentComplete
            ? `浏览器网络表已为 ${selectedWithGround.length} 个带 GND 引脚的模块分配端点；这不是电气连通性测试。`
            : "浏览器网络表缺少一个或多个预期的 GND 端点。",
    },
  ];

  const i2cTargets = design.selectedModules
    .filter(({ module }) => module.id !== "esp32-devkit-v1" && module.interfaces.includes("i2c"))
    .map(({ module }) => module.id);
  if (i2cTargets.length > 0) {
    const i2cNets = design.nets.filter((net) => net.kind === "i2c");
    const i2cCovered = i2cTargets.every((moduleId) =>
      i2cNets.some((net) => net.from.moduleId === moduleId || net.to.some((endpoint) => endpoint.moduleId === moduleId)),
    );
    erc.push({
      level: i2cCovered && i2cNets.length >= 2 ? "pass" : "fail",
      code: "INTENT-I2C-001",
      message:
        locale === "en"
          ? i2cCovered && i2cNets.length >= 2
            ? `The design-intent netlist includes SDA/SCL endpoints for ${i2cTargets.length} I2C modules; KiCad ERC remains required.`
            : "The design-intent netlist is missing expected I2C SDA/SCL endpoints."
          : i2cCovered && i2cNets.length >= 2
            ? `设计意图网络表包含 ${i2cTargets.length} 个 I2C 模块的 SDA/SCL 端点；仍需运行 KiCad ERC。`
            : "设计意图网络表缺少预期的 I2C SDA/SCL 端点。",
    });
  }

  const uartTargets = design.selectedModules
    .filter(({ module }) => module.id !== "esp32-devkit-v1" && module.interfaces.includes("uart"))
    .map(({ module }) => module.id);
  if (uartTargets.length > 0) {
    const uartNets = design.nets.filter((net) => net.kind === "uart");
    const uartCovered = uartTargets.every((moduleId) =>
      uartNets.some((net) => net.from.moduleId === moduleId || net.to.some((endpoint) => endpoint.moduleId === moduleId)),
    );
    erc.push({
      level: uartCovered ? "pass" : "fail",
      code: "INTENT-UART-001",
      message:
        locale === "en"
          ? uartCovered
            ? `The design-intent netlist includes UART endpoints for ${uartTargets.length} modules; pin direction still requires KiCad review.`
            : "The design-intent netlist is missing expected UART endpoints."
          : uartCovered
            ? `设计意图网络表包含 ${uartTargets.length} 个模块的 UART 端点；引脚方向仍需在 KiCad 中复核。`
            : "设计意图网络表缺少预期的 UART 端点。",
    });
  }

  if (!design.selectedModules.some(({ module }) => module.pins.some((pin) => pin.type === "power_out"))) {
    erc.push({ level: "fail", code: "INTENT-PWR-001", message: locale === "en" ? "The selected modules do not provide a power-source output pin." : "所选模块没有提供电源输出引脚。" });
  }

  const minimumSketchWidth = Math.min(...design.tracks.map((track) => track.widthMm));
  const drc: CheckItem[] = [
    {
      level: "warn",
      code: "INTENT-WIDTH-001",
      message:
        locale === "en"
          ? `The browser routing sketch uses widths down to ${minimumSketchWidth.toFixed(2)} mm; validate real copper widths with KiCad DRC.`
          : `浏览器走线草图的最小线宽为 ${minimumSketchWidth.toFixed(2)} mm；真实铜线宽度必须用 KiCad DRC 验证。`,
    },
    {
      level: "warn",
      code: "INTENT-CLEARANCE-001",
      message:
        locale === "en"
          ? "The browser preview does not compute copper clearance. KiCad DRC is authoritative."
          : "浏览器预览不计算铜箔间距，必须以 KiCad DRC 为准。",
    },
    ...moduleMechanicalChecks(design, locale),
  ];

  if (design.keepouts.length > 0) {
    drc.push({
      level: "warn",
      code: "DRC-KEEP-001",
      message: locale === "en" ? "Wireless/airflow keepouts were generated and still need final human review." : "已生成无线/气流 keepout，需要人工最终审阅。",
    });
  }

  const dfm: CheckItem[] = [
    { level: "warn", code: "INTENT-FAB-001", message: locale === "en" ? "A conservative JLC 2-layer profile is the design intent; confirm the actual fab rules in KiCad." : "设计意图采用保守的 JLC 2 层板参数；请在 KiCad 中确认实际制造规则。" },
    {
      level: "pass",
      code: "DFM-SIZE-001",
      message:
        locale === "en"
          ? `Board outline ${design.boardSizeMm.w} mm x ${design.boardSizeMm.h} mm is suitable for small-batch prototyping and product validation.`
          : `板框 ${design.boardSizeMm.w} mm x ${design.boardSizeMm.h} mm，适合小批量打样和产品验证。`,
    },
    {
      level: "warn",
      code: "DFM-EDA-001",
      message: locale === "en" ? "The exported project still needs footprint, layer, and manufacturing rule review inside JLC EDA." : "导出工程需要在 JLC EDA 内复核封装、层设置和制造规则。",
    },
  ];

  const all = [...erc, ...drc, ...dfm];
  return {
    erc,
    drc,
    dfm,
    summary: {
      pass: all.filter((item) => item.level === "pass").length,
      warn: all.filter((item) => item.level === "warn").length,
      fail: all.filter((item) => item.level === "fail").length,
    },
  };
}
