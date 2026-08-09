import type { SelectedModule } from "../domain/types";

type Locale = "zh" | "en";

export interface HarvestProfile {
  moduleId: string;
  moduleName: string;
  confidence: number;
  sourceLabel: string;
  assetStrategy: string;
  extracted: Array<{
    label: string;
    status: "done" | "review" | "generated";
    value: string;
  }>;
  nextReview: string;
}

function confidenceFor(status: SelectedModule["module"]["reviewStatus"]) {
  if (status === "verified") return 96;
  if (status === "needs_review") return 82;
  return 74;
}

function sourceLabel(moduleId: string, locale: Locale) {
  if (locale === "en") {
    if (moduleId.includes("esp32")) return "LCSC/EasyEDA cache + community module data";
    if (moduleId.includes("lora")) return "Vendor page cache + manual RF constraint template";
    if (moduleId.includes("tp4056")) return "LCSC search cache + common charger template";
    if (moduleId.includes("jst") || moduleId.includes("qwiic")) return "Connector spec cache + parametric footprint";
    return "Module library cache + datasheet field extraction";
  }

  if (moduleId.includes("esp32")) return "LCSC/EasyEDA 缓存 + 社区模块资料";
  if (moduleId.includes("lora")) return "供应商页面缓存 + 人工 RF 约束模板";
  if (moduleId.includes("tp4056")) return "LCSC 搜索缓存 + 常用充电模块模板";
  if (moduleId.includes("jst") || moduleId.includes("qwiic")) return "连接器规格缓存 + 参数化封装";
  return "模块库缓存 + datasheet 字段提取";
}

function assetStrategy(moduleId: string, locale: Locale) {
  if (locale === "en") {
    if (moduleId.includes("lora")) return "Parametric 3D + auto antenna keepout";
    if (moduleId.includes("oled")) return "Parametric 3D + black display panel";
    if (moduleId.includes("usb") || moduleId.includes("tp4056")) return "Parametric 3D + connector bump model";
    return "Parametric 3D model, replace with real STEP/GLB later";
  }

  if (moduleId.includes("lora")) return "参数化 3D + 天线 keepout 自动生成";
  if (moduleId.includes("oled")) return "参数化 3D + 显示屏黑色面板";
  if (moduleId.includes("usb") || moduleId.includes("tp4056")) return "参数化 3D + 连接器凸起模型";
  return "参数化 3D 模型，真实 STEP/GLB 后续替换";
}

export function buildHarvestProfiles(selectedModules: SelectedModule[], locale: Locale = "zh"): HarvestProfile[] {
  return selectedModules.map(({ module }) => {
    const needsReview = module.reviewStatus !== "verified";
    return {
      moduleId: module.id,
      moduleName: module.name,
      confidence: confidenceFor(module.reviewStatus),
      sourceLabel: sourceLabel(module.id, locale),
      assetStrategy: assetStrategy(module.id, locale),
      extracted: [
        {
          label: locale === "en" ? "Size" : "尺寸",
          status: "done",
          value: `${module.sizeMm.w} x ${module.sizeMm.h} mm`,
        },
        {
          label: locale === "en" ? "Connector" : "接口",
          status: "done",
          value: module.interfaces.join(" / "),
        },
        {
          label: locale === "en" ? "Pin map" : "Pin map",
          status: needsReview ? "review" : "done",
          value: `${module.pins.length} pins`,
        },
        {
          label: locale === "en" ? "3D model" : "3D 模型",
          status: module.model3d.mode === "parametric" ? "generated" : "done",
          value: module.model3d.mode === "parametric" ? (locale === "en" ? "Parametric generated" : "参数化生成") : locale === "en" ? "Cached model" : "缓存模型",
        },
      ],
      nextReview: needsReview ? module.reviewNotes : locale === "en" ? "Ready to generate the board." : "可直接进入底板生成。",
    };
  });
}

export function buildHarvestManifest(selectedModules: SelectedModule[], locale: Locale = "zh") {
  return {
    generated_at: new Date().toISOString(),
    mode: "offline-cache-first",
    note:
      locale === "en"
        ? "Uses cached module metadata and local parametric 3D assets for a stable Windows-native workflow."
        : "使用缓存模块元数据和本地参数化 3D 资产，保持 Windows 原生流程稳定。",
    modules: buildHarvestProfiles(selectedModules, locale),
  };
}
