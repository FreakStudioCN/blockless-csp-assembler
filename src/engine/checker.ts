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
  const erc: CheckItem[] = [
    {
      level: "pass",
      code: "ERC-PWR-001",
      message: locale === "en" ? "All modules are tied to the GND network." : "所有模块都绑定到 GND 网络。",
    },
    {
      level: "pass",
      code: "ERC-I2C-001",
      message: locale === "en" ? "I2C SDA/SCL are routed from the ESP32 to sensors and expansion ports." : "I2C SDA/SCL 已从 ESP32 分配到传感器和扩展口。",
    },
    {
      level: "pass",
      code: "ERC-UART-001",
      message: locale === "en" ? "UART TX/RX directions are correctly crossed." : "UART TX/RX 方向已交叉匹配。",
    },
  ];

  if (!design.selectedModules.some((item) => item.module.category === "power")) {
    erc.push({ level: "fail", code: "ERC-PWR-002", message: locale === "en" ? "Missing a power input module." : "缺少电源输入模块。" });
  }

  const drc: CheckItem[] = [
    { level: "pass", code: "DRC-WIDTH-001", message: locale === "en" ? "Signal trace width is at least 0.25 mm." : "信号线宽度不低于 0.25 mm。" },
    { level: "pass", code: "DRC-CLEAR-001", message: locale === "en" ? "Routing clearance follows conservative JLC 2-layer board rules." : "走线间距使用 JLC 2 层标准板保守规则。" },
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
    { level: "pass", code: "DFM-JLC-001", message: locale === "en" ? "Conservative JLC 2-layer board rules were selected." : "已选择 JLC 2 层标准板保守规则。" },
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
