import type { SelectedModule } from "../domain/types";

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

function sourceLabel(moduleId: string) {
  if (moduleId.includes("esp32")) return "LCSC/EasyEDA缓存 + 社区模块资料";
  if (moduleId.includes("lora")) return "供应商页面缓存 + 人工RF约束模板";
  if (moduleId.includes("tp4056")) return "LCSC搜索缓存 + 常用充电模块模板";
  if (moduleId.includes("jst") || moduleId.includes("qwiic")) return "连接器规格缓存 + 参数化封装";
  return "模块库缓存 + datasheet字段提取";
}

function assetStrategy(moduleId: string) {
  if (moduleId.includes("lora")) return "参数化3D + 天线keepout自动生成";
  if (moduleId.includes("oled")) return "参数化3D + 显示屏黑色面板";
  if (moduleId.includes("usb") || moduleId.includes("tp4056")) return "参数化3D + 连接器凸起模型";
  return "参数化3D模型，真实STEP/GLB后续替换";
}

export function buildHarvestProfiles(selectedModules: SelectedModule[]): HarvestProfile[] {
  return selectedModules.map(({ module }) => {
    const needsReview = module.reviewStatus !== "verified";
    return {
      moduleId: module.id,
      moduleName: module.name,
      confidence: confidenceFor(module.reviewStatus),
      sourceLabel: sourceLabel(module.id),
      assetStrategy: assetStrategy(module.id),
      extracted: [
        {
          label: "尺寸",
          status: "done",
          value: `${module.sizeMm.w} x ${module.sizeMm.h} mm`,
        },
        {
          label: "接口",
          status: "done",
          value: module.interfaces.join(" / "),
        },
        {
          label: "Pin map",
          status: needsReview ? "review" : "done",
          value: `${module.pins.length} pins`,
        },
        {
          label: "3D模型",
          status: module.model3d.mode === "parametric" ? "generated" : "done",
          value: module.model3d.mode === "parametric" ? "参数化生成" : "缓存模型",
        },
      ],
      nextReview: needsReview ? module.reviewNotes : "可直接进入底板生成。",
    };
  });
}

export function buildHarvestManifest(selectedModules: SelectedModule[]) {
  return {
    generated_at: new Date().toISOString(),
    mode: "offline-cache-first",
    note: "Uses cached module metadata and local parametric 3D assets for a stable Windows-native workflow.",
    modules: buildHarvestProfiles(selectedModules),
  };
}
