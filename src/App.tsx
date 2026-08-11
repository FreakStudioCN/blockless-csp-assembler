import { useCallback, useMemo, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { ThreeMFLoader } from "three/examples/jsm/loaders/3MFLoader.js";
import { useEffect, useRef } from "react";
import { demoCases, defaultPrompt, defaultPromptEn } from "./data/caseRegistry";
import { moduleLibrary } from "./data/modules";
import { moduleMarketplace } from "./data/marketplace";
import { generateDesign } from "./engine/generate";
import { buildHarvestProfiles } from "./engine/harvester";
import { clampPlacement, getPinPoint, pcbToScene } from "./engine/geometry";
import { routeNets } from "./engine/router";
import { checkDesign } from "./engine/checker";
import {
  buildEnclosureOpenScad,
  buildEnclosureSpec,
  defaultEnclosureSettings,
  type EnclosureSettings,
  type EnclosureSpec,
} from "./engine/enclosure";
import { buildBomCsv, buildEasyEdaProjectJson, buildReportJson, downloadExportZip } from "./engine/exporter";
import type { BoardDesign, CheckItem, DemoCase, HardwareModule, Placement, TrackSegment } from "./domain/types";

const steps = ["Prompt", "Plan", "Review", "Generate", "Export"] as const;
type Step = (typeof steps)[number];

const trackColors: Record<TrackSegment["kind"], string> = {
  power: "#d24b3a",
  gnd: "#20252b",
  i2c: "#2368c4",
  uart: "#229862",
  gpio: "#8064c9",
  spi: "#b76b1c",
};
const ROUTE_DRAG_THROTTLE_MS = 110;
const THREE_MAX_PIXEL_RATIO = 1.5;
const stlGeometryCache = new Map<string, THREE.BufferGeometry>();

function routeViaPoints(track: TrackSegment) {
  if (track.layer !== "bottom" || track.points.length < 2) return [];
  return [track.points[0], track.points[track.points.length - 1]];
}

type BoardLayer = "top" | "bottom";
type KicadPreviewTab = BoardLayer | "appearance";
type SketchView = BoardLayer | "three";
type GeneratePreviewMode = "kicad" | "interactive" | "enclosure";
type RouteRunStatus = "idle" | "running" | "ok" | "error";
type Locale = "zh" | "en";
type RouteMessageKey = "idle" | "running" | "ok" | "waitServer" | "waitBackend" | "error";
type RouteRunState = {
  status: RouteRunStatus;
  messageKey: RouteMessageKey;
  detail?: string;
};
type GenerationPhaseKey = "intent" | "modules" | "placement" | "routing" | "exports";
type GenerationRunState = {
  active: boolean;
  phaseIndex: number;
  progress: number;
};
type CncRunStatus = "idle" | "running" | "ready" | "error";
type CncPreviewLayer = "kicad-top" | "gerber-front" | "masked-front" | "traced-front" | "processed-front" | "processed-back" | "drill";
type CamFileEntry = {
  name: string;
  kind: string;
  bytes: number;
  url: string;
  path?: string;
};
type CamManifest = {
  ok: boolean;
  case: string;
  sourcePcb: string;
  gerberDir: string;
  gcodeDir: string;
  tool: string;
  generatedAt: string;
  files: CamFileEntry[];
};
type CamApiState = {
  status: "idle" | "running" | "ok" | "error";
  output?: string;
  manifest?: CamManifest | null;
};

const LOCALE_STORAGE_KEY = "blockless-pcb-locale";

type MountingHole = {
  id: string;
  x: number;
  y: number;
  diameterMm: number;
};

const GRID_SNAP_MM = 2.54;

const generationPhases: Array<{
  key: GenerationPhaseKey;
  title: Record<Locale, string>;
  detail: Record<Locale, string>;
}> = [
  {
    key: "intent",
    title: { zh: "LLM 解析需求", en: "LLM parses the brief" },
    detail: {
      zh: "提取供电、通信、传感器、接口和外形约束。",
      en: "Extracting power, radio, sensor, connector, and outline constraints.",
    },
  },
  {
    key: "modules",
    title: { zh: "模块选型与接口匹配", en: "Module selection and interface matching" },
    detail: {
      zh: "匹配商用模块、引脚、电压域和采购资料。",
      en: "Matching commercial modules, pins, voltage domains, and sourcing data.",
    },
  },
  {
    key: "placement",
    title: { zh: "约束布局求解", en: "Constraint placement solve" },
    detail: {
      zh: "按天线、USB、传感器朝向、固定孔和板边间距生成布局。",
      en: "Solving placement with antenna, USB, sensor, mounting-hole, and edge constraints.",
    },
  },
  {
    key: "routing",
    title: { zh: "顶层/底层自动布线", en: "Top/bottom autorouting" },
    detail: {
      zh: "生成层规划、过孔和净间距规则，并准备 KiCad/KRT 路由输出。",
      en: "Building layer plans, vias, clearance rules, and KiCad/KRT routing output.",
    },
  },
  {
    key: "exports",
    title: { zh: "工程包与制造文件", en: "Project package and manufacturing files" },
    detail: {
      zh: "汇总 JLC EDA、BOM、检查报告、3D 预览和 CAM 数据。",
      en: "Assembling JLC EDA, BOM, check reports, 3D preview, and CAM data.",
    },
  },
];

function snapMm(value: number) {
  return Math.round(value / GRID_SNAP_MM) * GRID_SNAP_MM;
}

function defaultMountingHoles(boardSize: { w: number; h: number }, diameterMm = 3.2): MountingHole[] {
  const inset = 4.2;
  return [
    { id: "mh-tl", x: inset, y: inset, diameterMm },
    { id: "mh-tr", x: boardSize.w - inset, y: inset, diameterMm },
    { id: "mh-bl", x: inset, y: boardSize.h - inset, diameterMm },
    { id: "mh-br", x: boardSize.w - inset, y: boardSize.h - inset, diameterMm },
  ];
}

function bilingual(locale: Locale, zh: string, en: string) {
  return locale === "en" ? en : zh;
}

function stepLabel(locale: Locale, step: Step) {
  const labels: Record<Locale, Record<Step, string>> = {
    zh: {
      Prompt: "输入",
      Plan: "选型",
      Review: "检查",
      Generate: "布局",
      Export: "导出",
    },
    en: {
      Prompt: "Prompt",
      Plan: "Plan",
      Review: "Review",
      Generate: "Generate",
      Export: "Export",
    },
  };
  return labels[locale][step];
}

function statusLabel(status: HardwareModule["reviewStatus"], locale: Locale) {
  const labels: Record<Locale, Record<HardwareModule["reviewStatus"], string>> = {
    zh: {
      verified: "已审阅",
      needs_review: "需复核",
      placeholder: "参数化",
    },
    en: {
      verified: "Verified",
      needs_review: "Needs review",
      placeholder: "Parametric",
    },
  };
  return labels[locale][status];
}

const routeRunMessages: Record<Locale, Record<RouteMessageKey, string>> = {
  zh: {
    idle: "本机 KiCad/KRT 就绪后可重新生成真实 PCB 布线。",
    running: "正在调用本机 KiCad/KRT，生成顶层/底层 SVG 和 3D PNG...",
    ok: "KiCad/KRT 已重新生成真实 PCB、顶层/底层 SVG 和 3D 渲染图。",
    waitServer: "本机布线已启动；当前 dev server 需要重启后才支持状态轮询。",
    waitBackend: "KiCad/KRT 仍在后台运行；完成后刷新 KiCad 真实输出即可看到新图。",
    error: "重新生成失败。",
  },
  en: {
    idle: "Regenerate the routed PCB once local KiCad/KRT is ready.",
    running: "Calling local KiCad/KRT to generate top/bottom SVGs and a 3D PNG...",
    ok: "KiCad/KRT regenerated the routed PCB, top/bottom SVGs, and 3D render.",
    waitServer: "Local routing has started; the current dev server needs a restart before status polling works.",
    waitBackend: "KiCad/KRT is still running in the background; refresh the KiCad output once it finishes.",
    error: "Regeneration failed.",
  },
};

const kicadRenderMeta: Record<
  string,
  {
    topSvg: string;
    bottomSvg: string;
    image: string;
    pcb: string;
    drcViolations: number;
    unconnectedPads: number;
  }
> = {
  "long-range-env-node": {
    topSvg: "/assets/kicad-svg/long-range-env-node-krt-routed-fcu.svg",
    bottomSvg: "/assets/kicad-svg/long-range-env-node-krt-routed-bcu.svg",
    image: "/assets/kicad-renders/long-range-env-node-krt-render.png",
    pcb: "generated/external-router-output/long-range-env-node-krt-routed.kicad_pcb",
    drcViolations: 0,
    unconnectedPads: 0,
  },
  "desktop-air-monitor": {
    topSvg: "/assets/kicad-svg/desktop-air-monitor-krt-routed-fcu.svg",
    bottomSvg: "/assets/kicad-svg/desktop-air-monitor-krt-routed-bcu.svg",
    image: "/assets/kicad-renders/desktop-air-monitor-krt-render.png",
    pcb: "generated/external-router-output/desktop-air-monitor-krt-routed.kicad_pcb",
    drcViolations: 0,
    unconnectedPads: 0,
  },
};

function moduleById(id: string): HardwareModule {
  const module = moduleLibrary.find((item) => item.id === id);
  if (!module) throw new Error(`Missing module: ${id}`);
  return module;
}

function caseById(id: string): DemoCase | undefined {
  return demoCases.find((item) => item.id === id);
}

function caseTitle(caseItem: DemoCase | undefined, locale: Locale) {
  if (!caseItem) return "";
  return locale === "en" ? caseItem.nameEn ?? caseItem.name : caseItem.name;
}

function casePrompt(caseItem: DemoCase | undefined, locale: Locale) {
  if (!caseItem) return "";
  return locale === "en" ? caseItem.promptEn ?? caseItem.prompt : caseItem.prompt;
}

function caseSummary(caseItem: DemoCase | undefined, locale: Locale) {
  if (!caseItem) return "";
  return locale === "en" ? caseItem.summaryEn ?? caseItem.summary ?? casePrompt(caseItem, locale) : caseItem.summary ?? caseItem.prompt;
}

function caseIndustry(caseItem: DemoCase | undefined, locale: Locale) {
  if (!caseItem) return "";
  return locale === "en" ? caseItem.industryEn ?? caseItem.industry ?? caseItem.intentTags[0] : caseItem.industry ?? caseItem.intentTags[0];
}

function caseStatus(caseItem: DemoCase | undefined, locale: Locale) {
  if (!caseItem) return "";
  return locale === "en" ? caseItem.statusEn ?? caseItem.status ?? "Ready" : caseItem.status ?? "可打开";
}

function caseOutputs(caseItem: DemoCase | undefined, locale: Locale) {
  if (!caseItem) return [];
  return locale === "en" ? caseItem.outputsEn ?? caseItem.outputs ?? [] : caseItem.outputs ?? [];
}

function caseModuleLabels(caseItem: DemoCase | undefined) {
  if (!caseItem) return [];
  return caseItem.preferredModules.slice(0, 4).map((moduleId) => moduleById(moduleId).model3d.label);
}

function matchesCasePrompt(value: string, caseItem: DemoCase | undefined) {
  if (!caseItem) return false;
  return value === caseItem.prompt || value === caseItem.promptEn;
}

function wait(ms: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, ms));
}

function downloadTextFile(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function sourceImageFor(module: HardwareModule) {
  const listing = moduleMarketplace[module.id]?.amazon;
  return listing?.imageUrl || module.model3d.imageUrl || "";
}

function moduleRetailLinks(module: HardwareModule) {
  const amazon = moduleMarketplace[module.id]?.amazon;
  const lcsc = moduleMarketplace[module.id]?.lcsc;
  const imageUrl = sourceImageFor(module);
  const sourceUrl = module.model3d.sourceUrl;
  const stepUrl = module.model3d.localStepUrl || module.model3d.stepUrl;
  const stlUrl = module.model3d.localUrl;

  return { amazon, lcsc, imageUrl, sourceUrl, stepUrl, stlUrl };
}

function ModuleInspector({ module, locale }: { module: HardwareModule; locale: Locale }) {
  const { amazon, lcsc, imageUrl, sourceUrl, stepUrl, stlUrl } = moduleRetailLinks(module);
  const t = (zh: string, en: string) => bilingual(locale, zh, en);
  const cadState =
    module.model3d.mode === "stl" || module.model3d.mode === "3mf"
      ? t("本地真实 3D 可加载", "Local real 3D available")
      : module.model3d.localStepUrl || module.model3d.stepUrl
        ? t("STEP 已缓存，浏览器使用参数化预览", "STEP cached, browser uses parametric preview")
        : t("参数化 3D fallback", "Parametric 3D fallback");

  return (
    <div className="module-inspector">
      <div className="module-inspector-media">
        {imageUrl ? (
          <img src={imageUrl} alt={module.name} loading="lazy" />
        ) : (
          <div className="source-card-fallback">{module.model3d.label}</div>
        )}
        <div className="module-inspector-meta">
          <span>{module.model3d.mode === "parametric" ? t("参数化 3D", "Parametric 3D") : t("真实 3D", "Real 3D")}</span>
          <span>{statusLabel(module.reviewStatus, locale)}</span>
          <span>{module.category}</span>
        </div>
      </div>
      <div className="module-inspector-body">
        <div className="module-inspector-head">
          <div>
            <strong>{module.name}</strong>
            <p>{module.description}</p>
          </div>
          <span className="module-inspector-price">${module.unitPriceUsd.toFixed(2)}</span>
        </div>
        <div className="module-chip-row">
          {module.interfaces.map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
        <div className="module-spec-grid">
          <div>
            <span>{t("接口", "Connector")}</span>
            <strong>{module.connector}</strong>
          </div>
          <div>
            <span>{t("电压", "Voltage")}</span>
            <strong>{module.voltage.join(" / ")}</strong>
          </div>
          <div>
            <span>{t("外形尺寸", "Footprint")}</span>
            <strong>
              {module.sizeMm.w} x {module.sizeMm.h} mm
            </strong>
          </div>
          <div>
            <span>{t("引脚数", "Pins")}</span>
            <strong>{module.pins.length}</strong>
          </div>
          <div>
            <span>{t("CAD 状态", "CAD status")}</span>
            <strong>{cadState}</strong>
          </div>
          <div>
            <span>{t("资料来源", "Source")}</span>
            <strong>{module.source}</strong>
          </div>
        </div>
        <div className="module-inspector-actions">
          {amazon ? (
            <a href={amazon.url} target="_blank" rel="noreferrer" className="source-button primary">
              {amazon.label}
            </a>
          ) : null}
          {lcsc ? (
            <a href={lcsc.url} target="_blank" rel="noreferrer" className="source-button">
              {lcsc.label}
            </a>
          ) : (
            <a href={module.purchaseUrl} target="_blank" rel="noreferrer" className="source-button">
              {t("采购模块", "Source module")}
            </a>
          )}
          {sourceUrl ? (
            <a href={sourceUrl} target="_blank" rel="noreferrer" className="source-button">
              {t("CAD 源", "CAD source")}
            </a>
          ) : null}
          {stepUrl ? (
            <a href={stepUrl} target="_blank" rel="noreferrer" className="source-button">
              {t("STEP", "STEP")}
            </a>
          ) : null}
          {stlUrl ? (
            <a href={stlUrl} target="_blank" rel="noreferrer" className="source-button">
              {t("STL", "STL")}
            </a>
          ) : null}
        </div>
        <div className="module-inspector-foot">
          <span>{t("接口", "Interfaces")}: {module.interfaces.join(" / ")}</span>
          <span>{t("引脚", "Pins")}: {module.pins.slice(0, 8).map((pin) => `${pin.name}${pin.net ? `:${pin.net}` : ""}`).join(" · ")}</span>
          <span>{statusLabel(module.reviewStatus, locale)}</span>
          <span>{module.reviewNotes}</span>
        </div>
      </div>
    </div>
  );
}

function SourceCard({ module, active = false, locale }: { module: HardwareModule; active?: boolean; locale: Locale }) {
  const { amazon, lcsc, imageUrl } = moduleRetailLinks(module);
  const t = (zh: string, en: string) => bilingual(locale, zh, en);

  return (
    <article className={`source-card ${active ? "active" : ""}`}>
      <div className="source-card-image">
        {imageUrl ? <img src={imageUrl} alt={module.name} loading="lazy" /> : <div className="source-card-fallback">{module.model3d.label}</div>}
      </div>
      <div className="source-card-body">
        <div className="source-card-head">
          <strong>{module.name}</strong>
          <span>${module.unitPriceUsd.toFixed(2)}</span>
        </div>
        <p>{module.connector}</p>
        <div className="source-card-badges">
          <span>{amazon?.hint ?? t("Amazon", "Amazon")}</span>
          <span>{lcsc?.hint ?? t("LCSC", "LCSC")}</span>
        </div>
        <div className="source-card-actions">
          {amazon ? (
            <a href={amazon.url} target="_blank" rel="noreferrer" className="source-button primary">
              {amazon.label}
            </a>
          ) : null}
          {lcsc ? (
            <a href={lcsc.url} target="_blank" rel="noreferrer" className="source-button">
              {lcsc.label}
            </a>
          ) : (
            <a href={module.purchaseUrl} target="_blank" rel="noreferrer" className="source-button">
              {t("购买", "Purchase")}
            </a>
          )}
        </div>
      </div>
    </article>
  );
}

function hexLuminance(hexColor: string) {
  const hex = hexColor.replace("#", "");
  if (hex.length !== 6) return 0.4;
  const [r, g, b] = [0, 2, 4].map((offset) => parseInt(hex.slice(offset, offset + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function moduleLabelStyle(module: HardwareModule) {
  const label = module.model3d.label;
  const maxWidth = Math.max(2, module.sizeMm.w - 2.2);
  const maxHeight = Math.max(2, module.sizeMm.h - 2.2);
  const fontSize = Math.max(2.15, Math.min(4, maxWidth / Math.max(1, label.length * 0.62), maxHeight * 0.42));
  const isLightBody = hexLuminance(module.model3d.bodyColor) > 0.58;
  return {
    fill: isLightBody ? "#1d3029" : "#ffffff",
    stroke: isLightBody ? "rgba(255,255,255,0.68)" : "rgba(18,31,26,0.44)",
    fontSize,
  };
}

function pseudoLaneY(netName: string, kind: TrackSegment["kind"], layer: BoardLayer, boardHeight: number) {
  if (layer === "bottom") {
    if (netName === "GND") return boardHeight * 0.78;
    if (netName.includes("LORA")) return boardHeight * 0.58;
    return boardHeight * 0.66;
  }
  if (netName === "3V3") return boardHeight * 0.28;
  if (netName === "I2C_SDA") return boardHeight * 0.36;
  if (netName === "I2C_SCL") return boardHeight * 0.43;
  if (netName === "BAT+") return boardHeight * 0.86;
  if (kind === "power") return boardHeight * 0.72;
  if (kind === "uart") return boardHeight * 0.5;
  return boardHeight * 0.55;
}

function BoardSvg({
  design,
  onPlacementChange,
  onSelectModule,
  onInspectModule,
  onBoardClick,
  mountingHoles = [],
  selectedModuleId,
  lockedModuleIds = [],
  layer = "top",
  mode = "routes",
  routeStyle = "normal",
  routeAnimationKey = 0,
  holePlacementEnabled = false,
}: {
  design: BoardDesign;
  onPlacementChange?: (moduleId: string, x: number, y: number) => void;
  onSelectModule?: (moduleId: string) => void;
  onInspectModule?: (moduleId: string) => void;
  onBoardClick?: (x: number, y: number) => void;
  mountingHoles?: MountingHole[];
  selectedModuleId?: string;
  lockedModuleIds?: string[];
  layer?: BoardLayer;
  mode?: "routes" | "ratsnest" | "layout";
  routeStyle?: "normal" | "pseudo";
  routeAnimationKey?: number;
  holePlacementEnabled?: boolean;
}) {
  const { w, h } = design.boardSizeMm;
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [drag, setDrag] = useState<{ moduleId: string; dx: number; dy: number } | null>(null);
  const [boardClickStart, setBoardClickStart] = useState<{ x: number; y: number } | null>(null);
  const lastModuleTapRef = useRef<{ moduleId: string; time: number } | null>(null);
  const lastDragEmitRef = useRef(0);
  const lockedSet = useMemo(() => new Set(lockedModuleIds), [lockedModuleIds]);
  const scaleStroke = (width: number) => Math.max(0.35, width);
  const pointerToBoard = (event: ReactPointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const transformed = point.matrixTransform(svg.getScreenCTM()?.inverse());
    return { x: transformed.x, y: transformed.y };
  };

  const onPointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!drag || !onPlacementChange) return;
    const pointer = pointerToBoard(event);
    const now = performance.now();
    if (now - lastDragEmitRef.current < ROUTE_DRAG_THROTTLE_MS) return;
    lastDragEmitRef.current = now;
    onPlacementChange(drag.moduleId, pointer.x - drag.dx, pointer.y - drag.dy);
  };

  const onSvgPointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    const target = event.target as Element;
    if (target.closest(".draggable-module")) return;
    lastModuleTapRef.current = null;
    setBoardClickStart(pointerToBoard(event));
  };

  const onSvgPointerUp = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (drag) {
      if (onPlacementChange) {
        const pointer = pointerToBoard(event);
        lastDragEmitRef.current = performance.now();
        onPlacementChange(drag.moduleId, pointer.x - drag.dx, pointer.y - drag.dy);
      }
      setDrag(null);
      setBoardClickStart(null);
      return;
    }

    if (holePlacementEnabled && boardClickStart && onBoardClick) {
      const pointer = pointerToBoard(event);
      if (Math.hypot(pointer.x - boardClickStart.x, pointer.y - boardClickStart.y) < 1.6) {
        onBoardClick(pointer.x, pointer.y);
      }
    }

    setBoardClickStart(null);
  };

  return (
    <svg
      ref={svgRef}
      className={`board-svg ${layer === "bottom" ? "bottom-layer" : "top-layer"} ${holePlacementEnabled ? "hole-mode" : ""}`}
      viewBox={`-4 -4 ${w + 8} ${h + 8}`}
      role="img"
      aria-label="2D PCB preview"
      onPointerDown={onSvgPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onSvgPointerUp}
      onPointerCancel={() => {
        lastDragEmitRef.current = 0;
        setDrag(null);
        setBoardClickStart(null);
      }}
    >
      <defs>
        <pattern id="grid" width="5" height="5" patternUnits="userSpaceOnUse">
          <path d="M 5 0 L 0 0 0 5" fill="none" stroke="#dce4df" strokeWidth="0.12" />
        </pattern>
      </defs>
      <rect x="0" y="0" width={w} height={h} rx="1.6" fill="#e9f2ed" stroke="#266447" strokeWidth="0.7" />
      <rect x="0" y="0" width={w} height={h} fill="url(#grid)" opacity="0.7" />

      {design.keepouts.map((keepout) => (
        <g key={keepout.label}>
          <rect
            x={keepout.x}
            y={keepout.y}
            width={keepout.w}
            height={keepout.h}
            fill="#fff1c2"
            stroke="#c48118"
            strokeWidth="0.45"
            strokeDasharray="1.2 1.1"
          />
          <text x={keepout.x + 1.2} y={keepout.y + 3.4} className="svg-note">
            keepout
          </text>
        </g>
      ))}

      {mountingHoles.map((hole) => (
        <g key={hole.id} className="mounting-hole">
          <circle
            cx={hole.x}
            cy={hole.y}
            r={hole.diameterMm / 2 + 1}
            fill="#e7d5a4"
            stroke="#8b6a24"
            strokeWidth="0.22"
          />
          <circle cx={hole.x} cy={hole.y} r={hole.diameterMm / 2} fill="#1f2a2d" />
          <circle
            cx={hole.x}
            cy={hole.y}
            r={hole.diameterMm / 2 + 1.9}
            fill="none"
            stroke="#8b6a24"
            strokeDasharray="0.8 0.8"
            strokeWidth="0.16"
          />
        </g>
      ))}

      {mode === "routes"
        ? routeStyle === "pseudo"
          ? design.tracks.filter((track) => track.layer === layer).map((track, index) => (
              <g key={`${routeAnimationKey}-${track.net}-${index}`} className="pseudo-route-group">
                <polyline
                  className="pseudo-route-halo"
                  points={track.points.map((point) => `${point.x},${point.y}`).join(" ")}
                  fill="none"
                  stroke={trackColors[track.kind]}
                  strokeWidth={Math.max(1.25, track.widthMm * 3.1)}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <polyline
                  className="pseudo-track route-rebuild"
                  points={track.points.map((point) => `${point.x},${point.y}`).join(" ")}
                  fill="none"
                  stroke={trackColors[track.kind]}
                  strokeWidth={Math.max(0.42, track.widthMm * 1.35)}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeDasharray="2.4 1.8"
                />
                {routeViaPoints(track).map((point, viaIndex) => (
                  <circle
                    key={`${track.net}-pseudo-via-${viaIndex}`}
                    className="pseudo-via route-rebuild"
                    cx={point.x}
                    cy={point.y}
                    r="0.55"
                  />
                ))}
              </g>
            ))
          : design.tracks.filter((track) => track.layer === layer).map((track, index) => (
              <g key={`${track.net}-${index}`}>
                <polyline
                  points={track.points.map((point) => `${point.x},${point.y}`).join(" ")}
                  fill="none"
                  stroke={trackColors[track.kind]}
                  strokeWidth={scaleStroke(track.widthMm)}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeDasharray={track.layer === "bottom" ? "1.2 1.2" : undefined}
                />
                {routeViaPoints(track).map((point, viaIndex) => (
                  <circle
                    key={`${track.net}-via-${viaIndex}`}
                    cx={point.x}
                    cy={point.y}
                    r="0.85"
                    fill="#c6963a"
                    stroke="#6b4a16"
                    strokeWidth="0.18"
                  />
                ))}
              </g>
            ))
        : mode === "ratsnest"
          ? design.nets.flatMap((net) => {
            const fromPlacement = design.placements.find((placement) => placement.moduleId === net.from.moduleId);
            if (!fromPlacement) return [];
            const fromModule = moduleById(net.from.moduleId);
            const from = getPinPoint(fromModule, fromPlacement, net.from.pin);
            return net.to.flatMap((target) => {
              const toPlacement = design.placements.find((placement) => placement.moduleId === target.moduleId);
              if (!toPlacement) return [];
              const toModule = moduleById(target.moduleId);
              const to = getPinPoint(toModule, toPlacement, target.pin);
              return (
                <line
                  key={`${net.net}-${target.moduleId}-${target.pin}`}
                  x1={from.x}
                  y1={from.y}
                  x2={to.x}
                  y2={to.y}
                  stroke={trackColors[net.kind]}
                  strokeWidth={Math.max(0.25, net.widthMm * 0.55)}
                  strokeDasharray="1.3 1"
                  strokeLinecap="round"
                  opacity="0.72"
                />
              );
            });
          })
          : null}

      {design.placements.flatMap((placement) => {
        const item = moduleById(placement.moduleId);
        return item.pins.map((pin) => {
          const point = getPinPoint(item, placement, pin.name);
          const outerPadRadius = routeStyle === "pseudo" ? 0.55 : 1.1;
          const innerPadRadius = routeStyle === "pseudo" ? 0.18 : 0.38;
          return (
            <g key={`${item.id}-${pin.name}-pad`}>
              <circle cx={point.x} cy={point.y} r={outerPadRadius} fill="#d1a13a" stroke="#705218" strokeWidth="0.14" />
              <circle cx={point.x} cy={point.y} r={innerPadRadius} fill="#24382f" />
            </g>
          );
        });
      })}

      {design.placements.map((placement) => {
        const item = moduleById(placement.moduleId);
        const width = placement.rotation === 90 || placement.rotation === 270 ? item.sizeMm.h : item.sizeMm.w;
        const height = placement.rotation === 90 || placement.rotation === 270 ? item.sizeMm.w : item.sizeMm.h;
        const isSelected = selectedModuleId === item.id;
        const isLocked = lockedSet.has(item.id);
        const labelStyle = moduleLabelStyle(item);
        return (
          <g
            key={item.id}
            className={`draggable-module ${isSelected ? "selected" : ""} ${isLocked ? "locked" : ""}`}
            transform={`translate(${placement.x} ${placement.y}) rotate(${placement.rotation})`}
            onPointerDown={(event) => {
              const now = Date.now();
              const previousTap = lastModuleTapRef.current;
              const isDoubleTap = Boolean(previousTap && previousTap.moduleId === item.id && now - previousTap.time < 320);
              lastModuleTapRef.current = { moduleId: item.id, time: now };
              onSelectModule?.(item.id);
              if (isDoubleTap) {
                onInspectModule?.(item.id);
                setDrag(null);
                setBoardClickStart(null);
                return;
              }
              if (!onPlacementChange || isLocked) return;
              const pointer = pointerToBoard(event as unknown as ReactPointerEvent<SVGSVGElement>);
              lastDragEmitRef.current = 0;
              setDrag({ moduleId: item.id, dx: pointer.x - placement.x, dy: pointer.y - placement.y });
              event.currentTarget.setPointerCapture(event.pointerId);
            }}
          >
            <rect
              x={-item.sizeMm.w / 2}
              y={-item.sizeMm.h / 2}
              width={item.sizeMm.w}
              height={item.sizeMm.h}
              rx="1.2"
              fill={item.model3d.bodyColor}
              stroke={isSelected ? "#f0b429" : "#17372c"}
              strokeWidth={isSelected ? "0.9" : "0.45"}
            />
            <rect
              x={-item.sizeMm.w / 2 + 1.2}
              y={-item.sizeMm.h / 2 + 1.2}
              width={Math.max(2, item.sizeMm.w - 2.4)}
              height={Math.max(2, item.sizeMm.h - 2.4)}
              rx="0.8"
              fill="none"
              stroke="rgba(255,255,255,0.62)"
              strokeWidth="0.25"
            />
            <text
              className="module-label"
              textAnchor="middle"
              dominantBaseline="middle"
              fill={labelStyle.fill}
              stroke={labelStyle.stroke}
              fontSize={labelStyle.fontSize}
            >
              {item.model3d.label}
            </text>
            <circle cx={-item.sizeMm.w / 2 + 2.2} cy={-item.sizeMm.h / 2 + 2.2} r="0.8" fill="#f8e9a1" />
            {isLocked ? (
              <text x={item.sizeMm.w / 2 - 5.2} y={-item.sizeMm.h / 2 + 3.7} className="lock-mark">
                LOCK
              </text>
            ) : null}
          </g>
        );
      })}

      {routeStyle !== "pseudo" ? design.placements.map((placement) => {
        const item = moduleById(placement.moduleId);
        const width = placement.rotation === 90 || placement.rotation === 270 ? item.sizeMm.h : item.sizeMm.w;
        const height = placement.rotation === 90 || placement.rotation === 270 ? item.sizeMm.w : item.sizeMm.h;
        return (
          <text key={`${item.id}-caption`} x={placement.x - width / 2} y={placement.y + height / 2 + 3} className="caption">
            {item.name}
          </text>
        );
      }) : null}
    </svg>
  );
}

function makeTextSprite(text: string) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 96;
  const context = canvas.getContext("2d")!;
  context.fillStyle = "rgba(255,255,255,0.92)";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#1f2933";
  context.font = "600 34px Arial";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(text, canvas.width / 2, canvas.height / 2);
  const texture = new THREE.CanvasTexture(canvas);
  const material = new THREE.SpriteMaterial({ map: texture });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(7, 2.6, 1);
  return sprite;
}

function makeRoundedRectShape(width: number, height: number, radius: number) {
  const x = -width / 2;
  const y = -height / 2;
  const shape = new THREE.Shape();
  shape.moveTo(x + radius, y);
  shape.lineTo(x + width - radius, y);
  shape.quadraticCurveTo(x + width, y, x + width, y + radius);
  shape.lineTo(x + width, y + height - radius);
  shape.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  shape.lineTo(x + radius, y + height);
  shape.quadraticCurveTo(x, y + height, x, y + height - radius);
  shape.lineTo(x, y + radius);
  shape.quadraticCurveTo(x, y, x + radius, y);
  return shape;
}

function makeTrackMesh(
  points: Array<{ x: number; y: number }>,
  boardSize: { w: number; h: number },
  scale: number,
  width: number,
  color: string,
  y: number,
  style: "normal" | "pseudo" = "normal",
) {
  const material = new THREE.MeshStandardMaterial({
    color,
    metalness: style === "pseudo" ? 0.22 : 0.45,
    roughness: style === "pseudo" ? 0.5 : 0.32,
    transparent: style === "pseudo",
    opacity: style === "pseudo" ? 0.78 : 1,
  });
  const group = new THREE.Group();

  for (let index = 0; index < points.length - 1; index += 1) {
    const a = pcbToScene(points[index], boardSize, scale);
    const b = pcbToScene(points[index + 1], boardSize, scale);
    const dx = b.x - a.x;
    const dz = b.z - a.z;
    const length = Math.hypot(dx, dz);
    const mesh = new THREE.Mesh(
      new THREE.BoxGeometry(length, style === "pseudo" ? 0.1 : 0.08, Math.max(0.4, width * scale * (style === "pseudo" ? 2.6 : 2))),
      material,
    );
    mesh.position.set((a.x + b.x) / 2, y, (a.z + b.z) / 2);
    mesh.rotation.y = -Math.atan2(dz, dx);
    group.add(mesh);
  }

  return group;
}

function disposeThreeObject(object: THREE.Object3D) {
  object.traverse((child) => {
    const mesh = child as THREE.Mesh;
    mesh.geometry?.dispose();
    const material = mesh.material as THREE.Material | THREE.Material[] | undefined;
    if (Array.isArray(material)) {
      material.forEach((item) => item.dispose());
    } else {
      material?.dispose();
    }
  });
}

function addParametricDetails(group: THREE.Group, item: HardwareModule, moduleW: number, moduleH: number, moduleHeight: number) {
  const chipMaterial = new THREE.MeshStandardMaterial({ color: "#1c242b", roughness: 0.55 });
  const metalMaterial = new THREE.MeshStandardMaterial({ color: "#d0a03c", metalness: 0.45, roughness: 0.35 });
  const plasticMaterial = new THREE.MeshStandardMaterial({ color: "#e7e2ce", roughness: 0.65 });

  if (item.category === "mcu" || item.category === "wireless") {
    const shield = new THREE.Mesh(new THREE.BoxGeometry(moduleW * 0.46, 1.25, moduleH * 0.28), new THREE.MeshStandardMaterial({ color: "#c4ccd2", metalness: 0.35, roughness: 0.28 }));
    shield.position.set(0, moduleHeight / 2 + 0.72, -moduleH * 0.12);
    group.add(shield);
    const usb = new THREE.Mesh(new THREE.BoxGeometry(moduleW * 0.3, 1.4, 3.6), new THREE.MeshStandardMaterial({ color: "#c9cfd4", metalness: 0.5, roughness: 0.24 }));
    usb.position.set(0, moduleHeight / 2 + 0.65, -moduleH / 2 - 0.6);
    group.add(usb);
  }

  if (item.category === "sensor") {
    const chip = new THREE.Mesh(new THREE.BoxGeometry(moduleW * 0.35, 1, moduleH * 0.38), chipMaterial);
    chip.position.set(0, moduleHeight / 2 + 0.6, 0);
    group.add(chip);
  }

  if (item.category === "power") {
    const connector = new THREE.Mesh(new THREE.BoxGeometry(moduleW * 0.48, 1.6, 3.8), metalMaterial);
    connector.position.set(0, moduleHeight / 2 + 0.9, -moduleH / 2 - 0.4);
    group.add(connector);
    const ic = new THREE.Mesh(new THREE.BoxGeometry(moduleW * 0.28, 0.8, moduleH * 0.22), chipMaterial);
    ic.position.set(0, moduleHeight / 2 + 0.6, moduleH * 0.08);
    group.add(ic);
  }

  if (item.category === "connector") {
    const body = new THREE.Mesh(new THREE.BoxGeometry(moduleW * 0.78, 1.8, moduleH * 0.72), plasticMaterial);
    body.position.set(0, moduleHeight / 2 + 0.85, 0);
    group.add(body);
  }

  if (item.category === "display") {
    const glass = new THREE.Mesh(new THREE.BoxGeometry(moduleW * 0.74, 0.45, moduleH * 0.48), new THREE.MeshStandardMaterial({ color: "#08121f", roughness: 0.2 }));
    glass.position.set(0, moduleHeight / 2 + 0.8, 0);
    group.add(glass);
  }
}

function makeBox(width: number, height: number, depth: number, color: string, metalness = 0.05, roughness = 0.48) {
  return new THREE.Mesh(
    new THREE.BoxGeometry(width, height, depth),
    new THREE.MeshStandardMaterial({ color, metalness, roughness }),
  );
}

function addPadRails(group: THREE.Group, moduleW: number, moduleH: number, topY: number, count: number) {
  const gold = new THREE.MeshStandardMaterial({ color: "#c9a45a", metalness: 0.42, roughness: 0.28 });
  const hole = new THREE.MeshStandardMaterial({ color: "#2c2418", roughness: 0.7 });
  for (let index = 0; index < count; index += 1) {
    const z = -moduleH * 0.42 + (index * moduleH * 0.84) / Math.max(1, count - 1);
    for (const x of [-moduleW * 0.42, moduleW * 0.42]) {
      const pad = new THREE.Mesh(new THREE.CylinderGeometry(0.52, 0.52, 0.12, 24), gold);
      pad.position.set(x, topY, z);
      group.add(pad);
      const drill = new THREE.Mesh(new THREE.CylinderGeometry(0.24, 0.24, 0.13, 18), hole);
      drill.position.set(x, topY + 0.02, z);
      group.add(drill);
    }
  }
}

function addCapacitorRow(group: THREE.Group, moduleW: number, moduleH: number, topY: number, rows = 2) {
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < 4; col += 1) {
      const cap = makeBox(1.1, 0.42, 0.72, col % 2 === 0 ? "#d1bd78" : "#d8d8cf", 0.18, 0.36);
      cap.position.set(-moduleW * 0.18 + col * 1.9, topY + 0.22, -moduleH * 0.16 + row * 3.1);
      group.add(cap);
    }
  }
}

function addRealColorDetails(group: THREE.Group, item: HardwareModule, moduleW: number, moduleH: number, topY: number) {
  addPadRails(group, moduleW, moduleH, topY + 0.05, item.category === "sensor" ? 4 : 12);

  if (item.id === "esp32-devkit-v1") {
    const shield = makeBox(moduleW * 0.56, 1.35, moduleH * 0.28, "#c9cdd0", 0.45, 0.22);
    shield.position.set(0, topY + 0.68, moduleH * 0.18);
    group.add(shield);

    const antenna = makeBox(moduleW * 0.86, 0.24, moduleH * 0.18, "#090909", 0.02, 0.55);
    antenna.position.set(0, topY + 0.2, moduleH * 0.4);
    group.add(antenna);
    for (let index = 0; index < 4; index += 1) {
      const trace = makeBox(moduleW * 0.09, 0.07, 0.22, "#f3f0e3", 0.2, 0.3);
      trace.position.set(-moduleW * 0.24 + index * moduleW * 0.16, topY + 0.36, moduleH * 0.4);
      trace.rotation.y = index % 2 === 0 ? 0.65 : -0.65;
      group.add(trace);
    }

    const usb = makeBox(moduleW * 0.48, 1.1, 3.0, "#cfd3d4", 0.5, 0.24);
    usb.position.set(0, topY + 0.55, -moduleH * 0.45);
    group.add(usb);
    addCapacitorRow(group, moduleW, moduleH, topY, 2);
  } else if (item.id === "lora-uart-ra02") {
    const rfBoard = makeBox(moduleW * 0.55, 0.34, moduleH * 0.36, "#14723d", 0.03, 0.5);
    rfBoard.position.set(0.5, topY + 0.24, -moduleH * 0.06);
    group.add(rfBoard);
    const rfChip = makeBox(moduleW * 0.2, 0.8, moduleH * 0.15, "#1c1c1c", 0.05, 0.42);
    rfChip.position.set(0.2, topY + 0.72, -moduleH * 0.08);
    group.add(rfChip);
    const crystal = makeBox(moduleW * 0.16, 0.52, moduleH * 0.08, "#d0d4d6", 0.42, 0.26);
    crystal.position.set(moduleW * 0.16, topY + 0.54, -moduleH * 0.22);
    group.add(crystal);
    addCapacitorRow(group, moduleW, moduleH, topY, 2);
  } else if (item.id === "bme280-i2c-board" || item.id === "sgp30-air-quality" || item.id === "aht20-i2c-board") {
    const sensor = makeBox(moduleW * 0.28, 0.72, moduleH * 0.24, "#232323", 0.04, 0.44);
    sensor.position.set(0, topY + 0.48, 0);
    group.add(sensor);
    addCapacitorRow(group, moduleW, moduleH, topY, 1);
  } else if (item.id === "tp4056-usb-c-charger" || item.id === "usb-c-5v-breakout") {
    const usb = makeBox(moduleW * 0.52, 1.25, moduleH * 0.18, "#cfd3d4", 0.55, 0.24);
    usb.position.set(0, topY + 0.62, -moduleH * 0.38);
    group.add(usb);
    const jst = makeBox(moduleW * 0.5, 1.2, moduleH * 0.22, "#333333", 0.02, 0.46);
    jst.position.set(0.1, topY + 0.6, moduleH * 0.26);
    group.add(jst);
    addCapacitorRow(group, moduleW, moduleH, topY, 2);
  } else if (item.category === "connector") {
    const shell = makeBox(moduleW * 0.72, 1.2, moduleH * 0.62, item.model3d.bodyColor, 0.02, 0.52);
    shell.position.set(0, topY + 0.62, 0);
    group.add(shell);
  } else if (item.category === "display") {
    const glass = makeBox(moduleW * 0.76, 0.42, moduleH * 0.48, "#06101b", 0.02, 0.18);
    glass.position.set(0, topY + 0.36, 0);
    group.add(glass);
  }
}

function Board3D({
  design,
  onPlacementChange,
  onSelectModule,
  onInspectModule,
  onBoardClick,
  mountingHoles = [],
  lockedModuleIds = [],
  showRoutes = true,
  routeStyle = "normal",
  holePlacementEnabled = false,
}: {
  design: BoardDesign;
  onPlacementChange?: (moduleId: string, x: number, y: number) => void;
  onSelectModule?: (moduleId: string) => void;
  onInspectModule?: (moduleId: string) => void;
  onBoardClick?: (x: number, y: number) => void;
  mountingHoles?: MountingHole[];
  lockedModuleIds?: string[];
  showRoutes?: boolean;
  routeStyle?: "normal" | "pseudo";
  holePlacementEnabled?: boolean;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const lastModuleTapRef = useRef<{ moduleId: string; time: number } | null>(null);

  useEffect(() => {
    const host = ref.current;
    if (!host) return;
    let disposed = false;

    const width = host.clientWidth;
    const height = Math.max(320, host.clientHeight);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#f7faf8");

    const camera = new THREE.PerspectiveCamera(36, width / height, 0.1, 1000);
    camera.position.set(0, 94, 78);
    camera.lookAt(0, 0, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, THREE_MAX_PIXEL_RATIO));
    host.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 52;
    controls.maxDistance = 160;
    controls.maxPolarAngle = Math.PI * 0.5;
    controls.target.set(0, 0, 0);

    const ambient = new THREE.AmbientLight(0xffffff, 1.25);
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(30, 60, 40);
    scene.add(ambient, key);

    const group = new THREE.Group();
    const scale = 0.78;
    const boardW = design.boardSizeMm.w * scale;
    const boardH = design.boardSizeMm.h * scale;
    const boardShape = makeRoundedRectShape(boardW, boardH, 1.8);
    const board = new THREE.Mesh(
      new THREE.ExtrudeGeometry(boardShape, { depth: 1.2, bevelEnabled: true, bevelSegments: 3, bevelSize: 0.22, bevelThickness: 0.18 }),
      new THREE.MeshStandardMaterial({ color: "#2f8b64", roughness: 0.78 }),
    );
    board.rotation.x = Math.PI / 2;
    board.position.y = -0.9;
    group.add(board);

    const edge = new THREE.EdgesGeometry(new THREE.BoxGeometry(boardW, 1.05, boardH));
    const edgeLine = new THREE.LineSegments(edge, new THREE.LineBasicMaterial({ color: "#17372c" }));
    edgeLine.position.y = -0.58;
    group.add(edgeLine);

    const copperY = 0.06;
    const lockedSet = new Set(lockedModuleIds);
    const routeRoot = new THREE.Group();

    for (const hole of mountingHoles) {
      const scenePoint = pcbToScene(hole, design.boardSizeMm, scale);
      const ring = new THREE.Mesh(
        new THREE.CylinderGeometry((hole.diameterMm / 2 + 1.1) * scale, (hole.diameterMm / 2 + 1.1) * scale, 0.12, 32),
        new THREE.MeshStandardMaterial({ color: "#d3aa55", metalness: 0.45, roughness: 0.28 }),
      );
      ring.position.set(scenePoint.x, 0.18, scenePoint.z);
      group.add(ring);

      const drill = new THREE.Mesh(
        new THREE.CylinderGeometry((hole.diameterMm / 2) * scale, (hole.diameterMm / 2) * scale, 0.16, 32),
        new THREE.MeshStandardMaterial({ color: "#1d282b", roughness: 0.75 }),
      );
      drill.position.set(scenePoint.x, 0.22, scenePoint.z);
      group.add(drill);
    }

    const clearRouteRoot = () => {
      while (routeRoot.children.length > 0) {
        const child = routeRoot.children[0];
        routeRoot.remove(child);
        disposeThreeObject(child);
      }
    };

    const drawRoutes = () => {
      clearRouteRoot();
      if (!showRoutes) return;
      for (const track of design.tracks) {
        const y = track.layer === "top" ? copperY : -0.15;
        routeRoot.add(makeTrackMesh(track.points, design.boardSizeMm, scale, track.widthMm, trackColors[track.kind], y, routeStyle));
        for (const point of routeViaPoints(track)) {
          const scenePoint = pcbToScene(point, design.boardSizeMm, scale);
          const via = new THREE.Mesh(
            new THREE.CylinderGeometry(0.75, 0.75, 0.18, 18),
            new THREE.MeshStandardMaterial({ color: "#c6963a", metalness: 0.5, roughness: 0.3 }),
          );
          via.position.set(scenePoint.x, 0.16, scenePoint.z);
          routeRoot.add(via);
        }
      }
    };

    const setRouteGhosted = (ghosted: boolean) => {
      routeRoot.traverse((child) => {
        const mesh = child as THREE.Mesh;
        const materials = Array.isArray(mesh.material) ? mesh.material : mesh.material ? [mesh.material] : [];
        materials.forEach((material) => {
          material.transparent = ghosted || routeStyle === "pseudo";
          material.opacity = ghosted ? 0.2 : routeStyle === "pseudo" ? 0.78 : 1;
        });
      });
    };

    group.add(routeRoot);
    drawRoutes();

    const loader = new STLLoader();
    const threeMfLoader = new ThreeMFLoader();
    const stlToBoardAxes = new THREE.Matrix4().set(
      0,
      -1,
      0,
      0,
      0,
      0,
      1,
      0,
      -1,
      0,
      0,
      0,
      0,
      0,
      0,
      1,
    );
    const moduleRoots: Array<{ id: string; root: THREE.Group }> = [];

    for (const placement of design.placements) {
      const item = moduleById(placement.moduleId);
      const moduleW = item.sizeMm.w * scale;
      const moduleH = item.sizeMm.h * scale;
      const moduleHeight = item.category === "connector" ? 3.2 : item.category === "power" ? 4 : 5;
      const hasRealModel = (item.model3d.mode === "stl" || item.model3d.mode === "3mf") && Boolean(item.model3d.localUrl);
      const moduleGroup = new THREE.Group();
      moduleGroup.position.set(
        (placement.x - design.boardSizeMm.w / 2) * scale,
        0.16,
        (placement.y - design.boardSizeMm.h / 2) * scale,
      );
      moduleGroup.rotation.y = (-placement.rotation * Math.PI) / 180;
      moduleGroup.userData.moduleId = item.id;
      moduleGroup.userData.boardX = placement.x;
      moduleGroup.userData.boardY = placement.y;
      moduleGroup.userData.locked = lockedSet.has(item.id);
      group.add(moduleGroup);
      moduleRoots.push({ id: item.id, root: moduleGroup });

      const substrateHeight = Math.max(0.65, moduleHeight * 0.22);
      const base = new THREE.Mesh(
        new THREE.BoxGeometry(moduleW, substrateHeight, moduleH),
        new THREE.MeshStandardMaterial({
          color: item.model3d.bodyColor,
          opacity: hasRealModel ? 0.16 : 1,
          transparent: hasRealModel,
          roughness: 0.62,
        }),
      );
      base.position.y = substrateHeight / 2;
      moduleGroup.add(base);

      if (item.model3d.mode === "3mf" && item.model3d.localUrl) {
        threeMfLoader.load(
          item.model3d.localUrl,
          (object) => {
            if (disposed) return;
            const fittedObject = object.clone(true);
            fittedObject.applyMatrix4(stlToBoardAxes);
            fittedObject.traverse((child) => {
              if (child instanceof THREE.Mesh) {
                child.material = new THREE.MeshStandardMaterial({
                  color: item.model3d.bodyColor,
                  roughness: 0.58,
                  metalness: 0.03,
                });
              }
            });
            fittedObject.updateMatrixWorld(true);
            const box = new THREE.Box3().setFromObject(fittedObject);
            const size = new THREE.Vector3();
            box.getSize(size);
            const fit = Math.min(moduleW / Math.max(size.x, 1), moduleH / Math.max(size.z, 1)) * 0.96;
            fittedObject.scale.setScalar(fit);
            const center = new THREE.Vector3();
            box.getCenter(center);
            fittedObject.position.set(-center.x * fit, moduleHeight * 0.22 + 0.08 - box.min.y * fit, -center.z * fit);
            moduleGroup.add(fittedObject);
          },
          undefined,
          () => {
            if (disposed) return;
            addParametricDetails(moduleGroup, item, moduleW, moduleH, moduleHeight);
          },
        );
      } else if (hasRealModel && item.model3d.localUrl) {
        const modelUrl = item.model3d.localUrl;
        const addStlGeometry = (geometry: THREE.BufferGeometry) => {
          if (disposed) return;
          const fittedGeometry = geometry.clone();
          fittedGeometry.applyMatrix4(stlToBoardAxes);
          fittedGeometry.computeVertexNormals();
          fittedGeometry.computeBoundingBox();
          const box = fittedGeometry.boundingBox;
          if (!box) return;
          const size = new THREE.Vector3();
          box.getSize(size);
          const fit = Math.min(moduleW / Math.max(size.x, 1), moduleH / Math.max(size.z, 1)) * 0.96;
          const stlMesh = new THREE.Mesh(
            fittedGeometry,
            new THREE.MeshStandardMaterial({ color: item.model3d.bodyColor, roughness: 0.58, metalness: 0.03 }),
          );
          stlMesh.scale.setScalar(fit);
          const center = new THREE.Vector3();
          box.getCenter(center);
          stlMesh.position.set(-center.x * fit, moduleHeight * 0.22 + 0.08 - box.min.y * fit, -center.z * fit);
          moduleGroup.add(stlMesh);
        };
        const cachedGeometry = stlGeometryCache.get(modelUrl);
        if (cachedGeometry) {
          addStlGeometry(cachedGeometry);
        } else {
          loader.load(
            modelUrl,
            (geometry) => {
              stlGeometryCache.set(modelUrl, geometry.clone());
              addStlGeometry(geometry);
            },
            undefined,
            () => {
              if (disposed) return;
              addParametricDetails(moduleGroup, item, moduleW, moduleH, moduleHeight);
            },
          );
        }
      } else {
        addParametricDetails(moduleGroup, item, moduleW, moduleH, moduleHeight);
      }

      const detailTopY = hasRealModel
        ? item.category === "sensor"
          ? 1.45
          : item.category === "power"
            ? 1.75
            : item.category === "wireless"
              ? 1.75
              : 1.9
        : moduleHeight / 2 + 1;
      addRealColorDetails(moduleGroup, item, moduleW, moduleH, detailTopY);

      const pinMaterial = new THREE.MeshStandardMaterial({ color: "#d8b65a", metalness: 0.3, roughness: 0.4 });
      for (const pinData of item.pins) {
        const pinPoint = getPinPoint(item, placement, pinData.name);
        const scenePoint = pcbToScene(pinPoint, design.boardSizeMm, scale);
        const pad = new THREE.Mesh(new THREE.CylinderGeometry(0.78, 0.78, 0.15, 18), pinMaterial);
        pad.position.set(scenePoint.x, 0.12, scenePoint.z);
        group.add(pad);

        const localPin = new THREE.Mesh(new THREE.BoxGeometry(0.7, 1.2, 0.7), pinMaterial);
        const localX =
          (scenePoint.x - moduleGroup.position.x) * Math.cos(moduleGroup.rotation.y) -
          (scenePoint.z - moduleGroup.position.z) * Math.sin(moduleGroup.rotation.y);
        const localZ =
          (scenePoint.x - moduleGroup.position.x) * Math.sin(moduleGroup.rotation.y) +
          (scenePoint.z - moduleGroup.position.z) * Math.cos(moduleGroup.rotation.y);
        localPin.position.set(localX, moduleHeight * 0.3 + 0.7, localZ);
        moduleGroup.add(localPin);
      }
    }

    scene.add(group);

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const hitPoint = new THREE.Vector3();
    const dragPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -0.16);
    let activeDrag: { moduleId: string; root: THREE.Group; offset: THREE.Vector3 } | null = null;
    let holeClickStart: { x: number; y: number; screenX: number; screenY: number } | null = null;

    const setPointer = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
    };

    const findModuleRoot = (object: THREE.Object3D) => {
      let cursor: THREE.Object3D | null = object;
      while (cursor) {
        if (typeof cursor.userData.moduleId === "string") return cursor as THREE.Group;
        cursor = cursor.parent;
      }
      return null;
    };

    const sceneToBoard = (point: THREE.Vector3) => ({
      x: point.x / scale + design.boardSizeMm.w / 2,
      y: point.z / scale + design.boardSizeMm.h / 2,
    });

    const onPointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      setPointer(event);
      const intersections = raycaster.intersectObjects(
        moduleRoots.map((item) => item.root),
        true,
      );
      const moduleRoot = intersections[0] ? findModuleRoot(intersections[0].object) : null;
      if (moduleRoot && raycaster.ray.intersectPlane(dragPlane, hitPoint)) {
        const now = Date.now();
        const previousTap = lastModuleTapRef.current;
        const isDoubleTap = Boolean(previousTap && previousTap.moduleId === moduleRoot.userData.moduleId && now - previousTap.time < 320);
        lastModuleTapRef.current = { moduleId: moduleRoot.userData.moduleId, time: now };
        onSelectModule?.(moduleRoot.userData.moduleId);
        if (isDoubleTap) {
          onInspectModule?.(moduleRoot.userData.moduleId);
          activeDrag = null;
          return;
        }
        if (!onPlacementChange || moduleRoot.userData.locked) return;
        controls.enabled = false;
        setRouteGhosted(true);
        renderer.domElement.setPointerCapture(event.pointerId);
        activeDrag = {
          moduleId: moduleRoot.userData.moduleId,
          root: moduleRoot,
          offset: hitPoint.clone().sub(moduleRoot.position),
        };
        return;
      }

      lastModuleTapRef.current = null;

      if (holePlacementEnabled && onBoardClick && raycaster.ray.intersectPlane(dragPlane, hitPoint)) {
        const boardPoint = sceneToBoard(hitPoint);
        if (
          boardPoint.x >= 0 &&
          boardPoint.x <= design.boardSizeMm.w &&
          boardPoint.y >= 0 &&
          boardPoint.y <= design.boardSizeMm.h
        ) {
          controls.enabled = false;
          renderer.domElement.setPointerCapture(event.pointerId);
          holeClickStart = { ...boardPoint, screenX: event.clientX, screenY: event.clientY };
        }
      }
    };

    const onPointerMove = (event: PointerEvent) => {
      if (!activeDrag) return;
      event.preventDefault();
      setPointer(event);
      if (!raycaster.ray.intersectPlane(dragPlane, hitPoint)) return;
      const next = hitPoint.clone().sub(activeDrag.offset);
      activeDrag.root.position.x = next.x;
      activeDrag.root.position.z = next.z;
    };

    const onPointerUp = (event: PointerEvent) => {
      if (activeDrag) {
        renderer.domElement.releasePointerCapture(event.pointerId);
        controls.enabled = true;
        const boardX = activeDrag.root.position.x / scale + design.boardSizeMm.w / 2;
        const boardY = activeDrag.root.position.z / scale + design.boardSizeMm.h / 2;
        setRouteGhosted(false);
        onPlacementChange?.(activeDrag.moduleId, boardX, boardY);
        activeDrag = null;
        return;
      }

      if (holeClickStart) {
        renderer.domElement.releasePointerCapture(event.pointerId);
        controls.enabled = true;
        if (Math.hypot(event.clientX - holeClickStart.screenX, event.clientY - holeClickStart.screenY) < 4) {
          onBoardClick?.(holeClickStart.x, holeClickStart.y);
        }
        holeClickStart = null;
      }
    };

    renderer.domElement.addEventListener("pointerdown", onPointerDown);
    renderer.domElement.addEventListener("pointermove", onPointerMove);
    renderer.domElement.addEventListener("pointerup", onPointerUp);
    renderer.domElement.addEventListener("pointercancel", onPointerUp);

    let raf = 0;
    const animate = () => {
      controls.update();
      renderer.render(scene, camera);
      raf = requestAnimationFrame(animate);
    };
    animate();

    const onResize = () => {
      if (!host) return;
      const nextWidth = host.clientWidth;
      const nextHeight = Math.max(320, host.clientHeight);
      camera.aspect = nextWidth / nextHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(nextWidth, nextHeight);
    };
    window.addEventListener("resize", onResize);

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      renderer.domElement.removeEventListener("pointermove", onPointerMove);
      renderer.domElement.removeEventListener("pointerup", onPointerUp);
      renderer.domElement.removeEventListener("pointercancel", onPointerUp);
      controls.dispose();
      disposeThreeObject(scene);
      renderer.dispose();
      host.removeChild(renderer.domElement);
    };
  }, [design, holePlacementEnabled, lockedModuleIds, mountingHoles, onBoardClick, onPlacementChange, onSelectModule, routeStyle, showRoutes]);

  return <div className={`three-host ${holePlacementEnabled ? "hole-mode" : ""}`} ref={ref} />;
}

function Enclosure3D({
  design,
  spec,
  animationKey,
}: {
  design: BoardDesign;
  spec: EnclosureSpec;
  animationKey: number;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const host = ref.current;
    if (!host) return;

    const width = host.clientWidth;
    const height = Math.max(420, host.clientHeight);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#f7faf8");

    const camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 1000);
    camera.position.set(42, 55, 86);
    camera.lookAt(0, 8, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, THREE_MAX_PIXEL_RATIO));
    host.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 48;
    controls.maxDistance = 155;
    controls.maxPolarAngle = Math.PI * 0.52;
    controls.target.set(0, 8, 0);

    const ambient = new THREE.AmbientLight(0xffffff, 1.35);
    const key = new THREE.DirectionalLight(0xffffff, 1.45);
    const rim = new THREE.DirectionalLight(0x92d9ff, 0.7);
    key.position.set(40, 70, 45);
    rim.position.set(-50, 45, -30);
    scene.add(ambient, key, rim);

    const root = new THREE.Group();
    const scale = 0.72;
    const boardW = design.boardSizeMm.w * scale;
    const boardH = design.boardSizeMm.h * scale;
    const wall = spec.settings.wallMm * scale;
    const clearance = spec.settings.clearanceMm * scale;
    const outerW = spec.outerSize.w * scale;
    const outerD = spec.outerSize.h * scale;
    const floorH = spec.settings.floorMm;
    const bodyH = spec.settings.baseHeightMm;
    const lidT = spec.settings.lidThicknessMm;
    const standoffH = spec.settings.standoffHeightMm;
    const boardLift = floorH + standoffH + 0.18;
    const topY = floorH + bodyH + 2.8;
    const animated = animationKey > 0;

    const enclosureMat = new THREE.MeshPhysicalMaterial({
      color: "#d8e3ea",
      metalness: 0.02,
      roughness: 0.34,
      transparent: true,
      opacity: 0.38,
      transmission: 0.18,
      thickness: 0.5,
    });
    const edgeMat = new THREE.LineBasicMaterial({ color: "#5c768b", transparent: true, opacity: 0.72 });
    const cutoutMat = new THREE.MeshStandardMaterial({
      color: "#06101b",
      roughness: 0.52,
      transparent: true,
      opacity: 0.76,
    });
    const floorMat = new THREE.MeshStandardMaterial({ color: "#dfe8ec", roughness: 0.48, transparent: true, opacity: 0.44 });
    const postMat = new THREE.MeshStandardMaterial({ color: "#cfd8df", roughness: 0.42, metalness: 0.06 });
    const screwMat = new THREE.MeshStandardMaterial({ color: "#18242b", roughness: 0.7 });

    const floor = new THREE.Mesh(new THREE.BoxGeometry(outerW, floorH, outerD), floorMat);
    floor.position.y = floorH / 2;
    root.add(floor);

    const wallMeshes: THREE.Mesh[] = [];
    const addWall = (geometry: THREE.BoxGeometry, position: THREE.Vector3) => {
      const mesh = new THREE.Mesh(geometry, enclosureMat.clone());
      mesh.position.copy(position);
      root.add(mesh);
      wallMeshes.push(mesh);
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geometry), edgeMat);
      edges.position.copy(position);
      root.add(edges);
      return mesh;
    };

    addWall(new THREE.BoxGeometry(outerW, bodyH, wall), new THREE.Vector3(0, floorH + bodyH / 2, -outerD / 2 + wall / 2));
    addWall(new THREE.BoxGeometry(outerW, bodyH, wall), new THREE.Vector3(0, floorH + bodyH / 2, outerD / 2 - wall / 2));
    addWall(new THREE.BoxGeometry(wall, bodyH, outerD), new THREE.Vector3(-outerW / 2 + wall / 2, floorH + bodyH / 2, 0));
    addWall(new THREE.BoxGeometry(wall, bodyH, outerD), new THREE.Vector3(outerW / 2 - wall / 2, floorH + bodyH / 2, 0));

    const postsRoot = new THREE.Group();
    for (const post of spec.standoffs) {
      const scenePoint = pcbToScene(post, design.boardSizeMm, scale);
      const postMesh = new THREE.Mesh(
        new THREE.CylinderGeometry((post.postDiameterMm * scale) / 2, (post.postDiameterMm * scale) / 2, post.heightMm, 36),
        postMat,
      );
      postMesh.position.set(scenePoint.x, floorH + post.heightMm / 2, scenePoint.z);
      postsRoot.add(postMesh);

      const hole = new THREE.Mesh(
        new THREE.CylinderGeometry((post.holeDiameterMm * scale) / 2, (post.holeDiameterMm * scale) / 2, 0.18, 28),
        screwMat,
      );
      hole.position.set(scenePoint.x, floorH + post.heightMm + 0.08, scenePoint.z);
      postsRoot.add(hole);
    }
    root.add(postsRoot);

    const boardGroup = new THREE.Group();
    boardGroup.position.y = boardLift;
    const boardShape = makeRoundedRectShape(boardW, boardH, 1.6);
    const board = new THREE.Mesh(
      new THREE.ExtrudeGeometry(boardShape, { depth: 1.15, bevelEnabled: true, bevelSegments: 2, bevelSize: 0.18, bevelThickness: 0.14 }),
      new THREE.MeshStandardMaterial({ color: "#2f8b64", roughness: 0.74 }),
    );
    board.rotation.x = Math.PI / 2;
    board.position.y = -0.58;
    boardGroup.add(board);

    const routeRoot = new THREE.Group();
    for (const track of design.tracks) {
      routeRoot.add(makeTrackMesh(track.points, design.boardSizeMm, scale, track.widthMm, trackColors[track.kind], track.layer === "top" ? 0.12 : -0.08, "pseudo"));
    }
    boardGroup.add(routeRoot);

    const loader = new STLLoader();
    const stlToBoardAxes = new THREE.Matrix4().set(
      0,
      -1,
      0,
      0,
      0,
      0,
      1,
      0,
      -1,
      0,
      0,
      0,
      0,
      0,
      0,
      1,
    );
    const moduleAnimations: Array<{
      root: THREE.Group;
      start: THREE.Vector3;
      end: THREE.Vector3;
      startRotation: number;
      endRotation: number;
    }> = [];
    const scatter = [
      new THREE.Vector3(-outerW * 0.55, 17, -outerD * 0.38),
      new THREE.Vector3(outerW * 0.54, 19, -outerD * 0.34),
      new THREE.Vector3(outerW * 0.48, 15, outerD * 0.42),
      new THREE.Vector3(-outerW * 0.5, 20, outerD * 0.37),
      new THREE.Vector3(-outerW * 0.02, 23, -outerD * 0.56),
      new THREE.Vector3(outerW * 0.08, 18, outerD * 0.56),
    ];

    for (const [index, placement] of design.placements.entries()) {
      const item = moduleById(placement.moduleId);
      const moduleW = item.sizeMm.w * scale;
      const moduleH = item.sizeMm.h * scale;
      const moduleHeight = item.category === "connector" ? 3.2 : item.category === "power" ? 4 : 5;
      const hasRealStl = item.model3d.mode === "stl" && Boolean(item.model3d.localUrl);
      const end = new THREE.Vector3(
        (placement.x - design.boardSizeMm.w / 2) * scale,
        0.16,
        (placement.y - design.boardSizeMm.h / 2) * scale,
      );
      const moduleGroup = new THREE.Group();
      moduleGroup.position.copy(animated ? scatter[index % scatter.length] : end);
      moduleGroup.rotation.y = animated ? -0.72 + index * 0.2 : (-placement.rotation * Math.PI) / 180;
      moduleGroup.scale.setScalar(animated ? 0.82 : 1);
      boardGroup.add(moduleGroup);
      moduleAnimations.push({
        root: moduleGroup,
        start: scatter[index % scatter.length].clone(),
        end,
        startRotation: -0.72 + index * 0.2,
        endRotation: (-placement.rotation * Math.PI) / 180,
      });

      const substrateHeight = Math.max(0.65, moduleHeight * 0.22);
      const base = new THREE.Mesh(
        new THREE.BoxGeometry(moduleW, substrateHeight, moduleH),
        new THREE.MeshStandardMaterial({
          color: item.model3d.bodyColor,
          opacity: hasRealStl ? 0.18 : 1,
          transparent: hasRealStl,
          roughness: 0.62,
        }),
      );
      base.position.y = substrateHeight / 2;
      moduleGroup.add(base);

      if (hasRealStl && item.model3d.localUrl) {
        loader.load(
          item.model3d.localUrl,
          (geometry) => {
            const fittedGeometry = geometry.clone();
            fittedGeometry.applyMatrix4(stlToBoardAxes);
            fittedGeometry.computeVertexNormals();
            fittedGeometry.computeBoundingBox();
            const box = fittedGeometry.boundingBox;
            if (!box) return;
            const size = new THREE.Vector3();
            box.getSize(size);
            const fit = Math.min(moduleW / Math.max(size.x, 1), moduleH / Math.max(size.z, 1)) * 0.96;
            const stlMesh = new THREE.Mesh(
              fittedGeometry,
              new THREE.MeshStandardMaterial({ color: item.model3d.bodyColor, roughness: 0.58, metalness: 0.03 }),
            );
            stlMesh.scale.setScalar(fit);
            const center = new THREE.Vector3();
            box.getCenter(center);
            stlMesh.position.set(-center.x * fit, moduleHeight * 0.22 + 0.08 - box.min.y * fit, -center.z * fit);
            moduleGroup.add(stlMesh);
          },
          undefined,
          () => {
            addParametricDetails(moduleGroup, item, moduleW, moduleH, moduleHeight);
          },
        );
      } else {
        addParametricDetails(moduleGroup, item, moduleW, moduleH, moduleHeight);
      }

      addRealColorDetails(
        moduleGroup,
        item,
        moduleW,
        moduleH,
        hasRealStl ? (item.category === "sensor" ? 1.45 : item.category === "power" ? 1.75 : 1.9) : moduleHeight / 2 + 1,
      );
    }
    root.add(boardGroup);

    const lidGroup = new THREE.Group();
    const lid = new THREE.Mesh(
      new THREE.BoxGeometry(outerW, lidT, outerD),
      new THREE.MeshPhysicalMaterial({
        color: "#e4eef2",
        roughness: 0.28,
        transparent: true,
        opacity: 0.46,
        transmission: 0.24,
        thickness: 0.5,
      }),
    );
    lid.position.y = lidT / 2;
    lidGroup.add(lid);
    const lidEdges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(outerW, lidT, outerD)), edgeMat);
    lidEdges.position.y = lidT / 2;
    lidGroup.add(lidEdges);

    const topCutoutRoot = new THREE.Group();
    for (const cutout of spec.topCutouts) {
      const scenePoint = pcbToScene(cutout, design.boardSizeMm, scale);
      const cutoutW = cutout.w * scale;
      const cutoutH = cutout.h * scale;
      const color =
        cutout.kind === "display-window" ? "#06111e" : cutout.kind === "antenna-window" ? "#1b2d37" : "#0f191a";
      const panel = new THREE.Mesh(
        new THREE.BoxGeometry(cutoutW, 0.08, cutoutH),
        new THREE.MeshStandardMaterial({ color, roughness: 0.38, transparent: true, opacity: 0.82 }),
      );
      panel.position.set(scenePoint.x, lidT + 0.05, scenePoint.z);
      topCutoutRoot.add(panel);

      const frame = new THREE.LineSegments(
        new THREE.EdgesGeometry(new THREE.BoxGeometry(cutoutW, 0.12, cutoutH)),
        new THREE.LineBasicMaterial({ color: cutout.kind === "sensor-vent" ? "#34d0b3" : "#61c8f4", transparent: true, opacity: 0.94 }),
      );
      frame.position.copy(panel.position);
      topCutoutRoot.add(frame);

      if (cutout.kind === "sensor-vent") {
        for (let slot = -2; slot <= 2; slot += 1) {
          const vent = new THREE.Mesh(new THREE.BoxGeometry(cutoutW * 0.78, 0.1, 0.38), cutoutMat);
          vent.position.set(scenePoint.x, lidT + 0.13, scenePoint.z + slot * cutoutH * 0.15);
          topCutoutRoot.add(vent);
        }
      }
    }
    lidGroup.add(topCutoutRoot);
    lidGroup.position.y = animated ? topY + 16 : topY;
    lidGroup.rotation.x = animated ? -0.16 : 0;
    root.add(lidGroup);

    const sideCutoutRoot = new THREE.Group();
    for (const cutout of spec.sideCutouts) {
      const center = (cutout.centerMm - (cutout.edge === "front" || cutout.edge === "back" ? design.boardSizeMm.w : design.boardSizeMm.h) / 2) * scale;
      const widthMm = cutout.widthMm * scale;
      const cutoutY = floorH + cutout.bottomMm + cutout.heightMm / 2;
      let mesh: THREE.Mesh;
      if (cutout.edge === "front" || cutout.edge === "back") {
        mesh = new THREE.Mesh(new THREE.BoxGeometry(widthMm, cutout.heightMm, 0.16), cutoutMat);
        mesh.position.set(center, cutoutY, cutout.edge === "front" ? -outerD / 2 - 0.04 : outerD / 2 + 0.04);
      } else {
        mesh = new THREE.Mesh(new THREE.BoxGeometry(0.16, cutout.heightMm, widthMm), cutoutMat);
        mesh.position.set(cutout.edge === "left" ? -outerW / 2 - 0.04 : outerW / 2 + 0.04, cutoutY, center);
      }
      sideCutoutRoot.add(mesh);
      const frame = new THREE.LineSegments(
        new THREE.EdgesGeometry(mesh.geometry),
        new THREE.LineBasicMaterial({ color: cutout.kind === "usb-port" ? "#61c8f4" : "#34d0b3", transparent: true, opacity: 0.92 }),
      );
      frame.position.copy(mesh.position);
      sideCutoutRoot.add(frame);
    }
    root.add(sideCutoutRoot);

    const clearanceBox = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(boardW + clearance * 2, 0.1, boardH + clearance * 2)),
      new THREE.LineBasicMaterial({ color: "#d2ad5d", transparent: true, opacity: 0.72 }),
    );
    clearanceBox.position.y = boardLift - 0.42;
    root.add(clearanceBox);

    scene.add(root);

    const ease = (value: number) => {
      const clamped = Math.min(1, Math.max(0, value));
      return clamped * clamped * (3 - 2 * clamped);
    };
    const startTime = performance.now();
    let raf = 0;
    const animate = () => {
      const elapsed = animated ? (performance.now() - startTime) / 1000 : 9;
      const placeP = ease((elapsed - 0.12) / 1.05);
      const shellP = ease((elapsed - 0.82) / 0.9);
      const cutoutP = ease((elapsed - 1.55) / 0.72);
      const lidP = ease((elapsed - 1.95) / 0.82);
      const routeP = ease((elapsed - 1.0) / 0.8);

      for (const item of moduleAnimations) {
        item.root.position.lerpVectors(item.start, item.end, placeP);
        item.root.rotation.y = item.startRotation + (item.endRotation - item.startRotation) * placeP;
        item.root.scale.setScalar(0.82 + placeP * 0.18);
        item.root.visible = placeP > 0.02;
      }
      routeRoot.visible = routeP > 0.02;
      routeRoot.scale.setScalar(0.92 + routeP * 0.08);
      postsRoot.visible = shellP > 0.08;
      postsRoot.scale.y = Math.max(0.08, shellP);
      postsRoot.position.y = floorH + (1 - shellP) * -2.2;
      for (const wallMesh of wallMeshes) {
        wallMesh.scale.y = Math.max(0.04, shellP);
        wallMesh.position.y = floorH + (bodyH * Math.max(0.04, shellP)) / 2;
      }
      sideCutoutRoot.visible = cutoutP > 0.03;
      sideCutoutRoot.scale.setScalar(0.82 + cutoutP * 0.18);
      topCutoutRoot.visible = cutoutP > 0.03;
      topCutoutRoot.scale.setScalar(0.82 + cutoutP * 0.18);
      lidGroup.position.y = topY + (1 - lidP) * 16;
      lidGroup.rotation.x = -0.16 * (1 - lidP);

      controls.update();
      renderer.render(scene, camera);
      raf = requestAnimationFrame(animate);
    };
    animate();

    const onResize = () => {
      const nextWidth = host.clientWidth;
      const nextHeight = Math.max(420, host.clientHeight);
      camera.aspect = nextWidth / nextHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(nextWidth, nextHeight);
    };
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      controls.dispose();
      renderer.dispose();
      host.removeChild(renderer.domElement);
    };
  }, [animationKey, design, spec]);

  return <div className="three-host enclosure-three-host" ref={ref} />;
}

function Stepper({ active, onChange, locale }: { active: Step; onChange: (step: Step) => void; locale: Locale }) {
  return (
    <div className="stepper">
      {steps.map((step) => (
        <button
          key={step}
          className={active === step ? "step active" : "step"}
          onClick={() => onChange(step)}
          aria-current={active === step ? "page" : undefined}
        >
          {stepLabel(locale, step)}
        </button>
      ))}
    </div>
  );
}

function GenerationProgressOverlay({ run, locale }: { run: GenerationRunState; locale: Locale }) {
  if (!run.active) return null;
  const t = (zh: string, en: string) => bilingual(locale, zh, en);
  const activePhase = generationPhases[Math.min(run.phaseIndex, generationPhases.length - 1)];

  return (
    <div className="generation-progress-overlay" role="status" aria-live="polite">
      <div className="generation-progress-card">
        <div className="generation-progress-visual" aria-hidden="true">
          <div className="progress-board">
            <span className="progress-chip main">AI</span>
            <span className="progress-chip radio">RF</span>
            <span className="progress-chip power">PWR</span>
            <span className="progress-chip sensor">SENS</span>
            <i className="progress-route one" />
            <i className="progress-route two" />
            <i className="progress-route three" />
          </div>
        </div>
        <div className="generation-progress-copy">
          <p className="eyebrow">{t("正在生成 PCB 工程", "Generating PCB project")}</p>
          <h3>{activePhase.title[locale]}</h3>
          <p>{activePhase.detail[locale]}</p>
          <div className="generation-progress-bar" aria-label={t("生成进度", "Generation progress")}>
            <span style={{ width: `${run.progress}%` }} />
          </div>
          <div className="generation-progress-meta">
            <strong>{run.progress}%</strong>
            <span>{t("布局、布线、导出链路准备中", "Placement, routing, and export chain in progress")}</span>
          </div>
        </div>
        <div className="generation-phase-list">
          {generationPhases.map((phase, index) => (
            <div
              className={`generation-phase ${index < run.phaseIndex ? "done" : ""} ${index === run.phaseIndex ? "active" : ""}`}
              key={phase.key}
            >
              <span>{index + 1}</span>
              <div>
                <strong>{phase.title[locale]}</strong>
                <small>{phase.detail[locale]}</small>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function PlanView({ design, locale }: { design: BoardDesign; locale: Locale }) {
  const t = (zh: string, en: string) => bilingual(locale, zh, en);

  return (
    <div className="grid two">
      <section className="panel">
        <h2>{t("AI 模块方案", "AI module plan")}</h2>
        <div className="module-list">
          {design.selectedModules.map(({ module, reason }) => (
            <article key={module.id} className="module-row">
              <div className="module-color" style={{ background: module.model3d.bodyColor }} />
              <div>
                <h3>{module.name}</h3>
                <p>{reason}</p>
              </div>
              <span>{module.category}</span>
            </article>
          ))}
        </div>
      </section>
      <section className="panel">
        <h2>{t("接口匹配", "Interface matching")}</h2>
        <div className="net-list">
          {design.nets.map((net) => (
            <div key={net.net} className="net-row">
              <strong>{net.net}</strong>
              <span>{net.from.moduleId}.{net.from.pin}</span>
              <span>{"->"}</span>
              <span>{net.to.map((target) => `${target.moduleId}.${target.pin}`).join(", ")}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function ReviewView({ design, locale }: { design: BoardDesign; locale: Locale }) {
  const harvestProfiles = buildHarvestProfiles(design.selectedModules, locale);
  const t = (zh: string, en: string) => bilingual(locale, zh, en);
  const averageConfidence = Math.round(
    harvestProfiles.reduce((sum, profile) => sum + profile.confidence, 0) / harvestProfiles.length,
  );

  return (
    <section className="panel">
      <div className="section-head">
        <div>
          <h2>{t("模块资料抓取 / 审阅", "Module harvest / review")}</h2>
          <p>{t("首版走缓存优先：自动补全尺寸、接口、Pin map、采购链接和参数化 3D 模型；人工只复核低置信项。", "Cache-first on the first pass: fill in dimensions, connectors, pin maps, purchase links, and parametric 3D models; humans only review low-confidence items.")}</p>
        </div>
        <div className="confidence-meter">
          <strong>{averageConfidence}%</strong>
          <span>{t("平均资料置信度", "Average data confidence")}</span>
        </div>
      </div>

      <div className="harvest-grid">
        {harvestProfiles.map((profile) => (
          <article className="harvest-card" key={profile.moduleId}>
            <div className="harvest-card-head">
              <strong>{profile.moduleName}</strong>
              <span>{profile.confidence}%</span>
            </div>
            <p>{profile.sourceLabel}</p>
            <div className="harvest-chips">
              {profile.extracted.map((item) => (
                <span className={`chip ${item.status}`} key={`${profile.moduleId}-${item.label}`}>
                  {item.label}: {item.value}
                </span>
              ))}
            </div>
            <small>{profile.assetStrategy}</small>
          </article>
        ))}
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>{t("模块", "Module")}</th>
              <th>{t("尺寸", "Size")}</th>
              <th>{t("接口", "Connector")}</th>
              <th>{t("电压", "Voltage")}</th>
              <th>{t("3D 状态", "3D status")}</th>
              <th>{t("审阅", "Review")}</th>
            </tr>
          </thead>
          <tbody>
            {design.selectedModules.map(({ module }) => (
              <tr key={module.id}>
                <td>
                  <strong>{module.name}</strong>
                  <small>{module.reviewNotes}</small>
                </td>
                <td>
                  <div className="inline-inputs">
                    <input aria-label={`${module.name} width`} defaultValue={module.sizeMm.w} />
                    <span>x</span>
                    <input aria-label={`${module.name} height`} defaultValue={module.sizeMm.h} />
                    <span>mm</span>
                  </div>
                </td>
                <td>{module.interfaces.join(" / ")}</td>
                <td>{module.voltage.join(" / ")}</td>
                <td>
                  <strong>{module.model3d.mode === "parametric" ? t("参数化 / STEP", "Parametric / STEP") : t("真实 3D", "Real 3D")}</strong>
                  <small>{module.model3d.localUrl ?? module.model3d.localStepUrl ?? t("等待抓取", "Waiting for harvest")}</small>
                </td>
                <td>
                  <span className={`badge ${module.reviewStatus}`}>{statusLabel(module.reviewStatus, locale)}</span>
                  <input aria-label={`${module.name} review note`} defaultValue={module.reviewNotes} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

type AssemblyPhase = "idle" | "placing" | "routing";

function GenerateMotionOverlay({ design, phase, locale }: { design: BoardDesign; phase: AssemblyPhase; locale: Locale }) {
  if (phase === "idle") return null;
  const t = (zh: string, en: string) => bilingual(locale, zh, en);

  const scatter = [
    { x: -12, y: 10 },
    { x: 112, y: 7 },
    { x: 105, y: 92 },
    { x: -9, y: 85 },
    { x: 55, y: -12 },
    { x: 58, y: 112 },
  ];

  return (
    <div className={`generate-motion-overlay ${phase}`}>
      <div className="motion-board">
        <svg viewBox={`0 0 ${design.boardSizeMm.w} ${design.boardSizeMm.h}`} className="motion-route-svg" aria-hidden="true">
          {design.tracks.slice(0, 18).map((track, index) => (
            <polyline
              key={`${track.net}-${index}`}
              className="motion-route"
              points={track.points.map((point) => `${point.x},${point.y}`).join(" ")}
              fill="none"
              stroke={trackColors[track.kind]}
              strokeWidth={Math.max(0.4, track.widthMm * 1.35)}
              strokeLinecap="round"
              strokeLinejoin="round"
              pathLength={1}
              style={{ "--delay": `${900 + index * 55}ms` } as CSSProperties}
            />
          ))}
        </svg>
        {design.placements.map((placement, index) => {
          const item = moduleById(placement.moduleId);
          const rotated = placement.rotation === 90 || placement.rotation === 270;
          const moduleW = rotated ? item.sizeMm.h : item.sizeMm.w;
          const moduleH = rotated ? item.sizeMm.w : item.sizeMm.h;
          const start = scatter[index % scatter.length];
          return (
            <div
              key={item.id}
              className="motion-module"
              style={
                {
                  "--from-x": `${start.x}%`,
                  "--from-y": `${start.y}%`,
                  "--to-x": `${(placement.x / design.boardSizeMm.w) * 100}%`,
                  "--to-y": `${(placement.y / design.boardSizeMm.h) * 100}%`,
                  "--rot": `${placement.rotation}deg`,
                  "--delay": `${index * 85}ms`,
                  width: `${(moduleW / design.boardSizeMm.w) * 100}%`,
                  height: `${(moduleH / design.boardSizeMm.h) * 100}%`,
                  background: item.model3d.bodyColor,
                } as CSSProperties
              }
            >
              {item.model3d.label}
            </div>
          );
        })}
      </div>
      <div className="motion-status">
        <strong>{phase === "placing" ? t("模块自动装配", "Auto module placement") : t("线路动态生成", "Dynamic routing")}</strong>
        <span>{phase === "placing" ? t("按约束把模块落到 PCB 底板", "Place modules onto the PCB from constraints") : t("根据当前布局重新生成顶层/底层草图布线", "Regenerate top/bottom sketch routes from the current layout")}</span>
      </div>
    </div>
  );
}

function GenerateView({
  design,
  generationKey,
  onPlacementChange,
  onRotateModule,
  onRerouteSketch,
  locale,
}: {
  design: BoardDesign;
  generationKey: number;
  onPlacementChange: (moduleId: string, x: number, y: number) => void;
  onRotateModule: (moduleId: string) => void;
  onRerouteSketch: () => void;
  locale: Locale;
}) {
  const t = (zh: string, en: string) => bilingual(locale, zh, en);
  const [previewMode, setPreviewMode] = useState<GeneratePreviewMode>("kicad");
  const [kicadTab, setKicadTab] = useState<KicadPreviewTab>("top");
  const [sketchView, setSketchView] = useState<SketchView>("top");
  const [selectedModuleId, setSelectedModuleId] = useState(design.placements[0]?.moduleId ?? "");
  const [lockedModuleIds, setLockedModuleIds] = useState<string[]>([]);
  const [snapToGrid, setSnapToGrid] = useState(true);
  const [holePlacementEnabled, setHolePlacementEnabled] = useState(false);
  const [holeDiameterMm, setHoleDiameterMm] = useState(3.2);
  const [mountingHoles, setMountingHoles] = useState<MountingHole[]>(() => defaultMountingHoles(design.boardSizeMm));
  const [enclosureSettings, setEnclosureSettings] = useState<EnclosureSettings>(defaultEnclosureSettings);
  const [enclosureAnimationKey, setEnclosureAnimationKey] = useState(0);
  const [enclosureStatus, setEnclosureStatus] = useState<"ready" | "running">("ready");
  const enclosureTimerRef = useRef<number | null>(null);
  const [assetVersion, setAssetVersion] = useState(() => Date.now());
  const [routeRun, setRouteRun] = useState<RouteRunState>({
    status: "idle",
    messageKey: "idle",
  });
  const [routingRevision, setRoutingRevision] = useState(0);
  const [assemblyPhase, setAssemblyPhase] = useState<AssemblyPhase>("idle");
  const kicadRender = kicadRenderMeta[design.id];
  const selectedPlacement = design.placements.find((placement) => placement.moduleId === selectedModuleId);
  const selectedModule = selectedPlacement ? moduleById(selectedPlacement.moduleId) : undefined;
  const mechanicalChecks = design.report.drc.filter((item) => item.code.startsWith("DRC-MECH"));
  const routeRunText = routeRun.detail ?? routeRunMessages[locale][routeRun.messageKey];
  const enclosureSpec = useMemo(
    () => buildEnclosureSpec(design, mountingHoles, enclosureSettings),
    [design, enclosureSettings, mountingHoles],
  );
  const enclosureScad = useMemo(() => buildEnclosureOpenScad(enclosureSpec), [enclosureSpec]);

  useEffect(() => {
    setSelectedModuleId(design.placements[0]?.moduleId ?? "");
    setLockedModuleIds([]);
    setMountingHoles(defaultMountingHoles(design.boardSizeMm));
    setHoleDiameterMm(3.2);
    setEnclosureAnimationKey(0);
    setEnclosureStatus("ready");
  }, [design.boardSizeMm.h, design.boardSizeMm.w, design.id]);

  useEffect(() => {
    if (generationKey <= 0) return undefined;
    setAssemblyPhase("placing");
    const routingTimer = window.setTimeout(() => setAssemblyPhase("routing"), 950);
    const doneTimer = window.setTimeout(() => setAssemblyPhase("idle"), 2450);
    return () => {
      window.clearTimeout(routingTimer);
      window.clearTimeout(doneTimer);
    };
  }, [design.id, generationKey]);

  useEffect(
    () => () => {
      if (enclosureTimerRef.current !== null) {
        window.clearTimeout(enclosureTimerRef.current);
      }
    },
    [],
  );

  const withAssetVersion = (url: string) => `${url}?v=${assetVersion}`;

  const handlePlacementChange = useCallback((moduleId: string, x: number, y: number) => {
    if (lockedModuleIds.includes(moduleId)) return;
    setSelectedModuleId(moduleId);
    setRoutingRevision((current) => current + 1);
    onPlacementChange(moduleId, snapToGrid ? snapMm(x) : x, snapToGrid ? snapMm(y) : y);
  }, [lockedModuleIds, onPlacementChange, snapToGrid]);

  const addMountingHole = useCallback((x: number, y: number) => {
    const margin = 3.2;
    const nextX = Math.min(design.boardSizeMm.w - margin, Math.max(margin, snapToGrid ? snapMm(x) : x));
    const nextY = Math.min(design.boardSizeMm.h - margin, Math.max(margin, snapToGrid ? snapMm(y) : y));
    setMountingHoles((current) => [
      ...current,
      {
        id: `mh-${Date.now()}-${current.length}`,
        x: Number(nextX.toFixed(2)),
        y: Number(nextY.toFixed(2)),
        diameterMm: holeDiameterMm,
      },
    ]);
  }, [design.boardSizeMm.h, design.boardSizeMm.w, holeDiameterMm, snapToGrid]);

  const toggleSelectedLock = () => {
    if (!selectedModuleId) return;
    setLockedModuleIds((current) =>
      current.includes(selectedModuleId)
        ? current.filter((moduleId) => moduleId !== selectedModuleId)
        : [...current, selectedModuleId],
    );
  };

  const rotateSelected = () => {
    if (!selectedModuleId || lockedModuleIds.includes(selectedModuleId)) return;
    setRoutingRevision((current) => current + 1);
    onRotateModule(selectedModuleId);
  };

  const regeneratePcbRouting = async () => {
    onRerouteSketch();
    setRoutingRevision((current) => current + 1);
    setAssemblyPhase("routing");
    window.setTimeout(() => setAssemblyPhase("idle"), 1900);
    setRouteRun({ status: "running", messageKey: "running" });
    try {
      const response = await fetch("/api/kicad/route", { method: "POST" });
      const result = (await response.json().catch(() => ({}))) as {
        ok?: boolean;
        code?: number | null;
        output?: string;
        message?: string;
        state?: { status?: RouteRunStatus; output?: string };
      };
      if (!response.ok || result.ok === false) {
        throw new Error(result.message ?? result.output ?? "KiCad/KRT routing failed.");
      }

      if (result.state?.status === "ok" || response.status === 200 || typeof result.code === "number") {
        setAssetVersion(Date.now());
        setRouteRun({ status: "ok", messageKey: "ok" });
        return;
      }

      for (let attempt = 0; attempt < 240; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
        const statusResponse = await fetch(`/api/kicad/status?t=${Date.now()}`);
        const contentType = statusResponse.headers.get("content-type") ?? "";
        if (!contentType.includes("application/json")) {
          setRouteRun({ status: "idle", messageKey: "waitServer" });
          return;
        }
        const statusResult = (await statusResponse.json().catch(() => ({}))) as {
          state?: { status?: RouteRunStatus; output?: string };
        };
        const status = statusResult.state?.status;
        if (status === "ok") {
          setAssetVersion(Date.now());
          setRouteRun({ status: "ok", messageKey: "ok" });
          return;
        }
        if (status === "error") {
          throw new Error(statusResult.state?.output ?? "KiCad/KRT routing failed.");
        }
      }

      setRouteRun({ status: "running", messageKey: "waitBackend" });
    } catch (error) {
      const message = error instanceof Error ? error.message : "重新生成失败。";
      setRouteRun({ status: "error", messageKey: "error", detail: message.slice(0, 260) });
    }
  };

  const updateEnclosureSetting = (key: keyof EnclosureSettings, value: number) => {
    setEnclosureSettings((current) => ({
      ...current,
      [key]: Number(value.toFixed(2)),
    }));
  };

  const openEnclosurePreview = () => {
    if (enclosureTimerRef.current !== null) {
      window.clearTimeout(enclosureTimerRef.current);
    }
    setPreviewMode("enclosure");
    setEnclosureStatus("running");
    setEnclosureAnimationKey((current) => current + 1);
    enclosureTimerRef.current = window.setTimeout(() => {
      setEnclosureStatus("ready");
      enclosureTimerRef.current = null;
    }, 2900);
  };

  const generateEnclosureCad = () => {
    openEnclosurePreview();
  };

  return (
    <div className="generate-workspace">
      <div className="generate-summary">
        <span>{t("工作台状态", "Workbench status")}: {previewMode === "kicad" ? t("真实输出", "real output") : previewMode === "interactive" ? t("交互布局", "interactive layout") : "CAD"}</span>
        <span>{t(`${design.selectedModules.length} 个模块`, `${design.selectedModules.length} modules`)}</span>
        <span>{t(`${design.nets.length} 条网络`, `${design.nets.length} nets`)}</span>
        <span>{t(`${mountingHoles.length} 孔 -> ${enclosureSpec.standoffs.length} 固定柱`, `${mountingHoles.length} holes -> ${enclosureSpec.standoffs.length} posts`)}</span>
        <span>{t(`${design.bom.length} 条 BOM`, `${design.bom.length} BOM lines`)}</span>
        <span>${design.bom.reduce((sum, line) => sum + line.unitPriceUsd * line.qty, 0).toFixed(2)}</span>
      </div>
      <div className="generate-main">
      <section className="panel visual-panel">
        <div className="panel-head">
          <h2>{t("KiCad 真实输出 / 可交互草图 / 外壳 CAD", "KiCad output / interactive sketch / enclosure CAD")}</h2>
          <div className="segmented-control preview-switch" aria-label={t("预览模式", "Preview mode")}>
            <button className={previewMode === "kicad" ? "active" : ""} onClick={() => setPreviewMode("kicad")}>
              {t("KiCad 真实输出", "KiCad output")}
            </button>
            <button
              className={previewMode === "interactive" ? "active" : ""}
              onClick={() => setPreviewMode("interactive")}
            >
              {t("可拖拽草图", "Interactive sketch")}
            </button>
            <button className={previewMode === "enclosure" ? "active" : ""} onClick={openEnclosurePreview}>
              {t("外壳 CAD", "Enclosure CAD")}
            </button>
          </div>
        </div>
        <div className="visual-stage">
          {previewMode === "enclosure" ? (
          <div className="enclosure-preview">
            <Enclosure3D design={design} spec={enclosureSpec} animationKey={enclosureAnimationKey} />
            <div className={`enclosure-readout ${enclosureStatus}`}>
              <span>{enclosureStatus === "running" ? t("正在生成外壳几何", "Generating enclosure geometry") : t("参数化外壳已就绪", "Parametric enclosure ready")}</span>
              <strong>
                {enclosureSpec.outerSize.w.toFixed(1)} x {enclosureSpec.outerSize.h.toFixed(1)} x {enclosureSpec.outerSize.d.toFixed(1)} mm
              </strong>
              <span>{t(`${enclosureSpec.standoffs.length} 个固定柱 / ${enclosureSpec.topCutouts.length} 个顶盖开孔 / ${enclosureSpec.sideCutouts.length} 个侧边开口`, `${enclosureSpec.standoffs.length} posts / ${enclosureSpec.topCutouts.length} top cutouts / ${enclosureSpec.sideCutouts.length} side openings`)}</span>
            </div>
            <div className={`enclosure-sequence ${enclosureStatus}`}>
              {[
                t("PCB 对位", "PCB datum"),
                t("固定柱", "Posts"),
                t("壳体墙", "Walls"),
                t("开孔", "Cutouts"),
                t("顶盖", "Lid"),
              ].map((label, index) => (
                <span key={label} style={{ "--step-delay": `${index * 210}ms` } as CSSProperties}>
                  {label}
                </span>
              ))}
            </div>
          </div>
          ) : previewMode === "kicad" && kicadRender ? (
          <div className="kicad-preview">
            <div className="layer-tabs" aria-label={t("KiCad 真实输出层", "KiCad output layers")}>
              <button className={kicadTab === "top" ? "active" : ""} onClick={() => setKicadTab("top")}>
                {t("顶层 F.Cu", "Top F.Cu")}
              </button>
              <button className={kicadTab === "bottom" ? "active" : ""} onClick={() => setKicadTab("bottom")}>
                {t("底层 B.Cu", "Bottom B.Cu")}
              </button>
              <button className={kicadTab === "appearance" ? "active" : ""} onClick={() => setKicadTab("appearance")}>
                {t("3D 外观", "3D appearance")}
              </button>
            </div>
            {kicadTab === "appearance" ? (
              <img
                className="kicad-appearance-view large"
                src={withAssetVersion(kicadRender.image)}
                alt={`${design.title} KiCad 3D appearance render`}
              />
            ) : (
              <img
                className="kicad-route-view"
                src={withAssetVersion(kicadTab === "top" ? kicadRender.topSvg : kicadRender.bottomSvg)}
                alt={`${design.title} KiCad routed PCB ${kicadTab} view`}
              />
            )}
            <div className="kicad-preview-meta">
              <span>{kicadTab === "top" ? t("KiCad/KRT 顶层真实 SVG", "KiCad/KRT top SVG") : kicadTab === "bottom" ? t("KiCad/KRT 底层真实 SVG", "KiCad/KRT bottom SVG") : t("KiCad 3D 真实渲染", "KiCad 3D render")}</span>
              <span>{t("KRT 自动布线 .kicad_pcb", "KRT autorouted .kicad_pcb")}</span>
              <span>{t(`${kicadRender.unconnectedPads} 个未连接焊盘`, `${kicadRender.unconnectedPads} unconnected pads`)}</span>
              <span>{t(`${kicadRender.drcViolations} 项 DRC 违规`, `${kicadRender.drcViolations} DRC violations`)}</span>
            </div>
            <p>{kicadRender.pcb}</p>
          </div>
          ) : (
          <div className="interactive-preview">
            <div className="layer-tabs" aria-label={t("草图视图", "Sketch view")}>
              <button className={sketchView === "top" ? "active" : ""} onClick={() => setSketchView("top")}>
                {t("顶层预布线", "Top sketch routes")}
              </button>
              <button className={sketchView === "bottom" ? "active" : ""} onClick={() => setSketchView("bottom")}>
                {t("底层预布线", "Bottom sketch routes")}
              </button>
              <button className={sketchView === "three" ? "active" : ""} onClick={() => setSketchView("three")}>
                {t("3D 拖拽", "3D drag")}
              </button>
            </div>
            {sketchView === "three" ? (
              <Board3D
                design={design}
                onPlacementChange={handlePlacementChange}
                onSelectModule={setSelectedModuleId}
                onInspectModule={setSelectedModuleId}
                onBoardClick={addMountingHole}
                mountingHoles={mountingHoles}
                lockedModuleIds={lockedModuleIds}
                showRoutes
                routeStyle="pseudo"
                holePlacementEnabled={holePlacementEnabled}
              />
            ) : (
              <BoardSvg
                design={design}
                onPlacementChange={handlePlacementChange}
                onSelectModule={setSelectedModuleId}
                onInspectModule={setSelectedModuleId}
                onBoardClick={addMountingHole}
                mountingHoles={mountingHoles}
                selectedModuleId={selectedModuleId}
                lockedModuleIds={lockedModuleIds}
                layer={sketchView}
                mode="routes"
                routeStyle="pseudo"
                routeAnimationKey={routingRevision}
                holePlacementEnabled={holePlacementEnabled}
              />
            )}
            <p className="interaction-hint">{t("草图用于实时布局、方向调整、固定孔和层规划；虚线会跟随模块位置更新，铜线以 KiCad/KRT 输出为准。", "The sketch is for live placement, rotation, mounting holes, and layer planning; dashed routes follow module positions while copper comes from KiCad/KRT output.")}</p>
          </div>
          )}
          <GenerateMotionOverlay design={design} phase={assemblyPhase} locale={locale} />
        </div>
        <div className="active-module-dock">
          <div className="panel-head">
            <div>
              <p className="eyebrow">{t("当前模块", "Active module")}</p>
              <h2>{selectedModule?.name ?? t("模块检查器", "Module inspector")}</h2>
            </div>
            {selectedModule ? <span className={`module-status ${selectedModule.reviewStatus}`}>{statusLabel(selectedModule.reviewStatus, locale)}</span> : null}
          </div>
          {selectedModule ? <ModuleInspector module={selectedModule} locale={locale} /> : <p className="module-inspector-empty">{t("未选择模块。", "No module selected.")}</p>}
        </div>
      </section>
      </div>
      <div className="generate-side">
      <section className="panel pcb-control-panel">
        <h2>{t("设计与采购", "Design and sourcing")}</h2>
        <div className="control-strip">
          <button className="primary" onClick={() => void regeneratePcbRouting()} disabled={routeRun.status === "running"}>
            {routeRun.status === "running" ? t("正在生成...", "Generating...") : t("重新生成 PCB 布线", "Regenerate PCB routing")}
          </button>
          <span className={`route-status ${routeRun.status}`}>{routeRunText}</span>
        </div>
        <div className="edit-controls">
          <div className="selected-module">
            <span>{t("选中模块", "Selected module")}</span>
            <strong>{selectedModule?.name ?? t("点击草图中的模块", "Click a module in the sketch")}</strong>
            {selectedPlacement ? <small>{selectedPlacement.rotation}° / {lockedModuleIds.includes(selectedPlacement.moduleId) ? t("已锁定", "Locked") : t("可移动", "Movable")}</small> : null}
          </div>
          <button onClick={rotateSelected} disabled={!selectedModule || lockedModuleIds.includes(selectedModule.id)}>
            {t("旋转 90°", "Rotate 90°")}
          </button>
          <button onClick={toggleSelectedLock} disabled={!selectedModule}>
            {selectedModule && lockedModuleIds.includes(selectedModule.id) ? t("解锁模块", "Unlock module") : t("锁定模块", "Lock module")}
          </button>
          <label className="toggle-control">
            <input type="checkbox" checked={snapToGrid} onChange={(event) => setSnapToGrid(event.target.checked)} />
            {t("2.54mm 吸附", "2.54 mm snap")}
          </label>
          <button className={holePlacementEnabled ? "active-tool" : ""} onClick={() => setHolePlacementEnabled((current) => !current)}>
            {holePlacementEnabled ? t("固定孔放置中", "Mounting-hole placement on") : t("放置固定孔", "Place mounting holes")}
          </button>
          <label className="number-control">
            {t("孔径", "Hole diameter")}
            <input
              aria-label={t("固定孔孔径", "Mounting hole diameter")}
              type="number"
              min="2"
              max="5"
              step="0.1"
              value={holeDiameterMm}
              onChange={(event) => {
                const next = Math.min(5, Math.max(2, Number(event.target.value) || holeDiameterMm));
                setHoleDiameterMm(next);
                setMountingHoles((current) => current.map((hole) => ({ ...hole, diameterMm: next })));
              }}
            />
            mm
          </label>
          <button onClick={() => setMountingHoles(defaultMountingHoles(design.boardSizeMm, holeDiameterMm))}>
            {t("四角孔", "Corner holes")}
          </button>
          <button onClick={() => setMountingHoles((current) => current.slice(0, -1))} disabled={mountingHoles.length === 0}>
            {t("撤销孔", "Undo hole")}
          </button>
        </div>
      </section>
      <section className="panel enclosure-cad-panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">Mechanical CAD</p>
            <h2>{t("外壳生成器", "Enclosure generator")}</h2>
          </div>
          <span className={`route-status ${enclosureStatus === "running" ? "running" : "ok"}`}>
            {enclosureStatus === "running" ? t("正在生成参数化外壳", "Generating parametric enclosure") : t("OpenSCAD 模型已就绪", "OpenSCAD model ready")}
          </span>
        </div>
        <div className="enclosure-settings-grid">
          <label className="number-control">
            {t("壁厚", "Wall")}
            <input
              aria-label={t("外壳壁厚", "Enclosure wall thickness")}
              type="number"
              min="1.2"
              max="4"
              step="0.1"
              value={enclosureSettings.wallMm}
              onChange={(event) => updateEnclosureSetting("wallMm", Math.min(4, Math.max(1.2, Number(event.target.value) || enclosureSettings.wallMm)))}
            />
            mm
          </label>
          <label className="number-control">
            {t("间隙", "Clearance")}
            <input
              aria-label={t("PCB 到外壳间隙", "PCB enclosure clearance")}
              type="number"
              min="1.5"
              max="8"
              step="0.1"
              value={enclosureSettings.clearanceMm}
              onChange={(event) => updateEnclosureSetting("clearanceMm", Math.min(8, Math.max(1.5, Number(event.target.value) || enclosureSettings.clearanceMm)))}
            />
            mm
          </label>
          <label className="number-control">
            {t("壳高", "Body height")}
            <input
              aria-label={t("外壳高度", "Enclosure body height")}
              type="number"
              min="10"
              max="32"
              step="0.5"
              value={enclosureSettings.baseHeightMm}
              onChange={(event) => updateEnclosureSetting("baseHeightMm", Math.min(32, Math.max(10, Number(event.target.value) || enclosureSettings.baseHeightMm)))}
            />
            mm
          </label>
          <label className="number-control">
            {t("顶盖", "Lid")}
            <input
              aria-label={t("顶盖厚度", "Lid thickness")}
              type="number"
              min="1.2"
              max="4"
              step="0.1"
              value={enclosureSettings.lidThicknessMm}
              onChange={(event) => updateEnclosureSetting("lidThicknessMm", Math.min(4, Math.max(1.2, Number(event.target.value) || enclosureSettings.lidThicknessMm)))}
            />
            mm
          </label>
        </div>
        <div className="enclosure-actions">
          <button className={`primary ${enclosureStatus === "running" ? "is-working" : ""}`} onClick={() => void generateEnclosureCad()} disabled={enclosureStatus === "running"}>
            {enclosureStatus === "running" ? t("生成中...", "Generating...") : t("生成外壳 CAD", "Generate enclosure CAD")}
          </button>
          <button onClick={() => downloadTextFile(`${design.id}-enclosure.scad`, enclosureScad, "text/plain")}>
            OpenSCAD
          </button>
          <button
            onClick={() =>
              downloadTextFile(
                `${design.id}-enclosure-manifest.json`,
                JSON.stringify(
                  {
                    title: design.title,
                    boardSizeMm: enclosureSpec.boardSize,
                    settings: enclosureSpec.settings,
                    outerSizeMm: enclosureSpec.outerSize,
                    innerSizeMm: enclosureSpec.innerSize,
                    standoffs: enclosureSpec.standoffs,
                    topCutouts: enclosureSpec.topCutouts,
                    sideCutouts: enclosureSpec.sideCutouts,
                    notes: enclosureSpec.notes,
                  },
                  null,
                  2,
                ),
                "application/json",
              )
            }
          >
            Manifest
          </button>
        </div>
        <div className="enclosure-metrics">
          <div>
            <span>{t("外形", "Outer size")}</span>
            <strong>
              {enclosureSpec.outerSize.w.toFixed(1)} x {enclosureSpec.outerSize.h.toFixed(1)} x {enclosureSpec.outerSize.d.toFixed(1)} mm
            </strong>
          </div>
          <div>
            <span>{t("固定柱", "Standoffs")}</span>
            <strong>{enclosureSpec.standoffs.length}</strong>
          </div>
          <div>
            <span>{t("开孔", "Cutouts")}</span>
            <strong>{enclosureSpec.topCutouts.length + enclosureSpec.sideCutouts.length}</strong>
          </div>
        </div>
        <div className="enclosure-cutout-list">
          {enclosureSpec.topCutouts.map((cutout) => (
            <small key={cutout.id}>
              {cutout.label}: {cutout.w.toFixed(1)} x {cutout.h.toFixed(1)} mm
            </small>
          ))}
          {enclosureSpec.sideCutouts.map((cutout) => (
            <small key={cutout.id}>
              {cutout.label}: {cutout.edge} / {cutout.widthMm.toFixed(1)} x {cutout.heightMm.toFixed(1)} mm
            </small>
          ))}
        </div>
        <pre className="enclosure-scad-preview">{enclosureScad.split("\n").slice(0, 12).join("\n")}</pre>
      </section>
      <section className="panel sourcing-panel">
        <h2>{t("模块栈 / 采购", "Module stack / sourcing")}</h2>
        <div className="source-summary">
          <span>{t(`${design.selectedModules.length} 个已选模块`, `${design.selectedModules.length} selected modules`)}</span>
          <span>{t("采购入口在 active module 中展开", "Sourcing opens in the active module card")}</span>
          <span>{t("真实 CAD 优先，缺失时参数化 fallback", "Real CAD first, parametric fallback when missing")}</span>
        </div>
        <div className="source-grid compact-source-grid">
          {design.selectedModules.map(({ module }) => (
            <SourceCard key={module.id} module={module} active={module.id === selectedModuleId} locale={locale} />
          ))}
        </div>
        <h2>{t("固定孔 -> 外壳固定柱", "Mounting holes -> enclosure posts")}</h2>
        <div className={`hole-summary ${holePlacementEnabled ? "active" : ""}`}>
          <strong>{t(`固定孔 ${mountingHoles.length} 个`, `${mountingHoles.length} mounting holes`)}</strong>
          <span>{holePlacementEnabled ? t("在 2D 或 3D 草图板面点击即可放置", "Click the 2D or 3D sketch board to place a hole") : t("打开放置模式后可继续加孔", "Turn on placement mode to add more holes")}</span>
          <div className="hole-coordinate-list">
            {mountingHoles.map((hole) => (
              <small key={hole.id}>
                {hole.x.toFixed(1)}, {hole.y.toFixed(1)} / {hole.diameterMm.toFixed(1)}mm
              </small>
            ))}
          </div>
          <div className="standoff-map">
            {enclosureSpec.standoffs.map((post) => (
              <small key={post.id}>
                {post.id}: {post.x.toFixed(1)}, {post.y.toFixed(1)} {"->"} post {post.postDiameterMm.toFixed(1)}mm / h{post.heightMm.toFixed(1)}mm
              </small>
            ))}
          </div>
        </div>
        <div className="mechanical-status">
          {mechanicalChecks.map((item) => (
            <div className={`mechanical-row ${item.level}`} key={item.code}>
              <span>{item.code}</span>
              <p>{item.message}</p>
            </div>
          ))}
        </div>
        <h2>{t("布局理由", "Placement reasons")}</h2>
        <div className="reason-list">
          {design.placements.map((placement) => (
            <p key={placement.moduleId}>
              <strong>{moduleById(placement.moduleId).name}</strong>：{placement.role}
            </p>
          ))}
        </div>
        <h2>{t("2D 布局草图", "2D layout sketch")}</h2>
        <div className="mini-board">
          <BoardSvg
            design={design}
            onPlacementChange={handlePlacementChange}
            onSelectModule={setSelectedModuleId}
            onInspectModule={setSelectedModuleId}
            mountingHoles={mountingHoles}
            selectedModuleId={selectedModuleId}
              lockedModuleIds={lockedModuleIds}
              mode="layout"
            />
        </div>
      </section>
      </div>
    </div>
  );
}

function CheckList({ title, items }: { title: string; items: CheckItem[] }) {
  return (
    <div className="check-group">
      <h3>{title}</h3>
      {items.map((item) => (
        <div className={`check ${item.level}`} key={item.code}>
          <span>{item.level.toUpperCase()}</span>
          <p>{item.message}</p>
        </div>
      ))}
    </div>
  );
}

function formatBytes(bytes: number) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function camFileLabel(kind: string, locale: Locale) {
  const labels: Record<string, Record<Locale, string>> = {
    "top-isolation": { zh: "顶层铜线隔离", en: "Top copper isolation" },
    "bottom-isolation": { zh: "底层铜线隔离", en: "Bottom copper isolation" },
    drill: { zh: "Excellon 钻孔循环", en: "Excellon drill cycle" },
    outline: { zh: "板框切割", en: "Board outline cut" },
  };
  return labels[kind]?.[locale] ?? kind;
}

function findCamFile(manifest: CamManifest | null, kind: string) {
  return manifest?.files.find((file) => file.kind === kind) ?? null;
}

function findCamFileByName(manifest: CamManifest | null, name: string) {
  return manifest?.files.find((file) => file.name === name) ?? null;
}

function CncToolpathPanel({ design, locale }: { design: BoardDesign; locale: Locale }) {
  const t = (zh: string, en: string) => bilingual(locale, zh, en);
  const [camStatus, setCamStatus] = useState<CncRunStatus>("idle");
  const [camManifest, setCamManifest] = useState<CamManifest | null>(null);
  const [camLog, setCamLog] = useState("");
  const [camPreviewLayer, setCamPreviewLayer] = useState<CncPreviewLayer>("processed-front");
  const [gcodeSample, setGcodeSample] = useState("");
  const kicadRender = kicadRenderMeta[design.id];
  const frontPreview = findCamFile(camManifest, "front-preview");
  const processedBackPreview = findCamFile(camManifest, "back-preview");
  const originalFrontPreview = findCamFileByName(camManifest, "outp1_original_front.svg");
  const maskedFrontPreview = findCamFileByName(camManifest, "outp4_masked_front.svg");
  const tracedFrontPreview = findCamFileByName(camManifest, "traced_front_final.svg");
  const drillPreview = findCamFileByName(camManifest, "original_drill.svg");
  const gcodeKinds = ["top-isolation", "bottom-isolation", "drill", "outline"];
  const gcodeFiles = gcodeKinds.map((kind) => findCamFile(camManifest, kind)).filter((file): file is CamFileEntry => Boolean(file));
  const topGcodeFile = findCamFile(camManifest, "top-isolation");
  const previewLayers: Array<{
    key: CncPreviewLayer;
    label: string;
    detail: string;
    url?: string;
    fileName: string;
  }> = [
    {
      key: "kicad-top",
      label: t("KiCad 顶层", "KiCad F.Cu"),
      detail: t("KiCad/KRT 路由 SVG", "KiCad/KRT routed SVG"),
      url: kicadRender?.topSvg,
      fileName: kicadRender?.topSvg.split("/").slice(-1)[0] ?? "KiCad top SVG",
    },
    {
      key: "gerber-front",
      label: t("Gerber 导入", "Gerber import"),
      detail: t("pcb2gcode 原始导入层", "pcb2gcode original import"),
      url: originalFrontPreview?.url,
      fileName: originalFrontPreview?.name ?? "outp1_original_front.svg",
    },
    {
      key: "masked-front",
      label: t("隔离掩膜", "Isolation mask"),
      detail: t("pcb2gcode masked front", "pcb2gcode masked front"),
      url: maskedFrontPreview?.url,
      fileName: maskedFrontPreview?.name ?? "outp4_masked_front.svg",
    },
    {
      key: "traced-front",
      label: t("路径追踪", "Traced routes"),
      detail: t("pcb2gcode traced final", "pcb2gcode traced final"),
      url: tracedFrontPreview?.url,
      fileName: tracedFrontPreview?.name ?? "traced_front_final.svg",
    },
    {
      key: "processed-front",
      label: t("顶层刀路", "Top toolpath"),
      detail: t("pcb2gcode processed final", "pcb2gcode processed final"),
      url: frontPreview?.url,
      fileName: frontPreview?.name ?? "processed_front_final.svg",
    },
    {
      key: "processed-back",
      label: t("底层刀路", "Bottom toolpath"),
      detail: t("pcb2gcode processed final", "pcb2gcode processed final"),
      url: processedBackPreview?.url,
      fileName: processedBackPreview?.name ?? "processed_back_final.svg",
    },
    {
      key: "drill",
      label: t("钻孔", "Drill"),
      detail: t("Excellon drill SVG", "Excellon drill SVG"),
      url: drillPreview?.url,
      fileName: drillPreview?.name ?? "original_drill.svg",
    },
  ];
  const selectedPreview = previewLayers.find((layer) => layer.key === camPreviewLayer && layer.url) ?? previewLayers.find((layer) => layer.key === "processed-front" && layer.url) ?? previewLayers.find((layer) => layer.url);
  const camStageLabels = [
    t("读取 KiCad PCB", "Read KiCad PCB"),
    "Gerber / Excellon",
    "pcb2gcode",
    t("写入 .ngc", "Write .ngc"),
  ];
  const statusText =
    camStatus === "running"
      ? t("正在调用 KiCad CLI + pcb2gcode...", "Calling KiCad CLI + pcb2gcode...")
      : camStatus === "ready"
        ? t("真实 CNC 刀路文件已就绪", "Real CNC toolpath files ready")
        : camStatus === "error"
          ? t("真实 CAM 生成失败", "Real CAM generation failed")
          : t("等待从真实 Gerber / Excellon 生成", "Waiting for real Gerber / Excellon conversion");

  useEffect(() => {
    setCamStatus("idle");
    setCamManifest(null);
    setCamLog("");
    setCamPreviewLayer("processed-front");
    setGcodeSample("");
    let cancelled = false;
    const loadExistingManifest = async () => {
      try {
        const response = await fetch(`/api/cam/status?case=${encodeURIComponent(design.id)}&t=${Date.now()}`);
        const result = (await response.json().catch(() => ({}))) as { state?: CamApiState };
        if (cancelled) return;
        if (result.state?.status === "ok" && result.state.manifest?.case === design.id) {
          setCamManifest(result.state.manifest);
          setCamStatus("ready");
          setCamLog(result.state.output ?? "");
        }
      } catch {
        if (!cancelled) setCamStatus("idle");
      }
    };
    void loadExistingManifest();
    return () => {
      cancelled = true;
    };
  }, [design.id]);

  useEffect(() => {
    let cancelled = false;
    if (!topGcodeFile) {
      setGcodeSample("");
      return () => {
        cancelled = true;
      };
    }

    const loadGcodeSample = async () => {
      try {
        const response = await fetch(`${topGcodeFile.url}?v=${encodeURIComponent(camManifest?.generatedAt ?? "")}`);
        const text = await response.text();
        if (!cancelled) {
          setGcodeSample(text.split(/\r?\n/).slice(0, 14).join("\n"));
        }
      } catch {
        if (!cancelled) setGcodeSample("");
      }
    };

    void loadGcodeSample();
    return () => {
      cancelled = true;
    };
  }, [camManifest?.generatedAt, topGcodeFile?.url]);

  const runCam = async () => {
    setCamStatus("running");
    setCamLog("");
    try {
      const startResponse = await fetch(`/api/cam/generate?case=${encodeURIComponent(design.id)}`, { method: "POST" });
      const startResult = (await startResponse.json().catch(() => ({}))) as { state?: CamApiState; message?: string };
      if (!startResponse.ok || startResult.state?.status === "error") {
        throw new Error(startResult.message ?? startResult.state?.output ?? "CAM export failed.");
      }

      for (let attempt = 0; attempt < 90; attempt += 1) {
        await wait(800);
        const statusResponse = await fetch(`/api/cam/status?case=${encodeURIComponent(design.id)}&t=${Date.now()}`);
        const statusResult = (await statusResponse.json().catch(() => ({}))) as { state?: CamApiState };
        const state = statusResult.state;
        setCamLog(state?.output ?? "");
        if (state?.status === "ok" && state.manifest?.case === design.id) {
          setCamManifest(state.manifest);
          setCamStatus("ready");
          return;
        }
        if (state?.status === "error") {
          throw new Error(state.output ?? "CAM export failed.");
        }
      }
      throw new Error("CAM export timed out.");
    } catch (error) {
      setCamStatus("error");
      setCamLog(error instanceof Error ? error.message : String(error));
    }
  };

  const fileRows = [
    {
      kind: "top-isolation",
      fallbackName: `${design.id}_front.ngc`,
    },
    {
      kind: "bottom-isolation",
      fallbackName: `${design.id}_back.ngc`,
    },
    {
      kind: "drill",
      fallbackName: `${design.id}_drill.ngc`,
    },
    {
      kind: "outline",
      fallbackName: `${design.id}_outline.ngc`,
    },
  ];

  return (
    <section className={`panel cnc-toolpath-panel ${camStatus}`}>
      <div className="panel-head">
        <div>
          <p className="eyebrow">CAM / CNC</p>
          <h2>{t("PCB 文件转 CNC 刀路", "PCB to CNC toolpaths")}</h2>
        </div>
        <span className={`route-status ${camStatus === "ready" ? "ok" : camStatus}`}>{statusText}</span>
      </div>
      <div className="cnc-toolpath-grid">
        <div className="cnc-viewer-column">
          <div className="cam-layer-tabs" aria-label={t("CAM 图层", "CAM layers")}>
            {previewLayers.map((layer) => (
              <button
                key={layer.key}
                className={camPreviewLayer === layer.key ? "active" : ""}
                onClick={() => setCamPreviewLayer(layer.key)}
                disabled={!layer.url || camStatus === "running"}
              >
                {layer.label}
              </button>
            ))}
          </div>
          <div className="cnc-preview-shell">
          {camStatus === "running" && kicadRender ? (
            <div className="cam-run-preview">
              <img
                className="cam-engineering-shot"
                src={kicadRender.topSvg}
                alt={`${design.title} KiCad top copper engineering view`}
              />
              <div className="cam-run-overlay">
                <span className="cam-run-badge">KiCad CLI + pcb2gcode</span>
                <strong>{t("正在从真实 PCB 工程生成刀路", "Generating toolpaths from the real PCB project")}</strong>
                <div className="cam-run-progress" aria-hidden="true">
                  <span />
                </div>
                <div className="cam-run-steps">
                  {camStageLabels.map((label, index) => (
                    <span key={label} style={{ "--step-delay": `${index * 260}ms` } as CSSProperties}>
                      {label}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          ) : selectedPreview?.url ? (
            <div className="cnc-engineering-preview">
              <img
                className={`cnc-real-preview ${["gerber-front", "masked-front", "traced-front", "drill"].includes(camPreviewLayer) ? "light-layer" : ""}`}
                src={`${selectedPreview.url}?v=${encodeURIComponent(camManifest?.generatedAt ?? "")}`}
                alt={`${design.title} ${selectedPreview.detail}`}
              />
              <div className="cam-layer-readout">
                <span>{selectedPreview.detail}</span>
                <strong>{selectedPreview.fileName}</strong>
                <span>{camPreviewLayer === "kicad-top" ? t("KiCad/KRT 源视图", "KiCad/KRT source view") : t("pcb2gcode 真实输出 SVG", "Real pcb2gcode output SVG")}</span>
              </div>
            </div>
          ) : (
            <div className="cnc-preview-empty">
              <strong>{t("等待真实 CAM 输出", "Waiting for real CAM output")}</strong>
              <span>{t("点击后从当前 KiCad PCB 导出 Gerber/Excellon，再由 pcb2gcode 生成 G-code 和 processed SVG。", "Click to export Gerber/Excellon from the current KiCad PCB, then let pcb2gcode generate G-code and processed SVGs.")}</span>
            </div>
          )}
          </div>
        </div>
        <div className="cnc-toolpath-body">
          <p>
            {t(
              "真实链路：KiCad CLI 从当前 .kicad_pcb 导出 Gerber/Excellon，pcb2gcode 读取这些制造文件并生成隔离铣、钻孔和板框切割 G-code。",
              "Real chain: KiCad CLI exports Gerber/Excellon from the current .kicad_pcb, and pcb2gcode reads those manufacturing files to generate isolation, drilling, and outline G-code.",
            )}
          </p>
          <div className="cam-pipeline">
            <span>{t("KiCad PCB", "KiCad PCB")}</span>
            <span>Gerber / Excellon</span>
            <span>pcb2gcode</span>
            <span>G-code / SVG</span>
          </div>
          <div className="cnc-file-list">
            {fileRows.map((row) => {
              const file = findCamFile(camManifest, row.kind);
              return (
                <div className={`cnc-file-row ${file ? "ready" : ""} ${camStatus === "running" ? "building" : ""}`} key={row.kind}>
                <strong>{file?.name ?? row.fallbackName}</strong>
                <span>{camFileLabel(row.kind, locale)}</span>
                <small>{camStatus === "running" ? t("正在写入", "Writing") : file ? formatBytes(file.bytes) : t("等待真实生成", "Pending real generation")}</small>
              </div>
              );
            })}
          </div>
          <div className="cnc-machine-settings">
            <span>{camManifest?.tool ?? "pcb2gcode 2.4.0"}</span>
            <span>{t("V-bit 0.20mm", "0.20 mm V-bit")}</span>
            <span>{t("隔离深度 0.08mm", "0.08 mm isolation depth")}</span>
            <span>{t("隔离 3 passes", "3 isolation passes")}</span>
            <span>{t("板框深度 1.70mm", "1.70 mm outline depth")}</span>
          </div>
          <div className="cam-evidence-grid">
            <div>
              <span>{t("单位", "Units")}</span>
              <strong>G21 / mm</strong>
            </div>
            <div>
              <span>{t("隔离铣刀", "Isolation bit")}</span>
              <strong>0.20 mm</strong>
            </div>
            <div>
              <span>{t("下刀深度", "Z work")}</span>
              <strong>-0.08 mm</strong>
            </div>
            <div>
              <span>{t("铣削进给", "Mill feed")}</span>
              <strong>260 mm/min</strong>
            </div>
            <div>
              <span>{t("钻孔深度", "Drill Z")}</span>
              <strong>-1.70 mm</strong>
            </div>
            <div>
              <span>{t("隔离 passes", "Isolation passes")}</span>
              <strong>3</strong>
            </div>
            <div>
              <span>{t("输出文件", "Output files")}</span>
              <strong>{camManifest ? `${camManifest.files.length} SVG / NGC` : t("等待生成", "Pending")}</strong>
            </div>
          </div>
          {gcodeSample ? (
            <div className="cam-gcode-sample">
              <span>{t("真实 .ngc 文件头", "Real .ngc header")}: {topGcodeFile?.name}</span>
              <pre>{gcodeSample}</pre>
            </div>
          ) : null}
          <div className="export-actions">
            <button className={`primary ${camStatus === "running" ? "is-working" : ""}`} onClick={runCam} disabled={camStatus === "running"}>
              {camStatus === "running" ? t("真实生成中...", "Generating real CAM...") : t("生成真实 CNC 刀路", "Generate real CNC toolpaths")}
            </button>
            {gcodeFiles.map((file) => (
              <a key={file.name} className="source-button" href={file.url} download>
                {file.kind.includes("isolation") ? file.kind.replace("-isolation", "") : file.kind}
              </a>
            ))}
          </div>
          {camManifest ? (
            <div className="cam-source-paths">
              <span>{t("源 PCB", "Source PCB")}: {camManifest.sourcePcb}</span>
              <span>{t("Gerber 输出", "Gerber output")}: {camManifest.gerberDir}</span>
              <span>{t("G-code 输出", "G-code output")}: {camManifest.gcodeDir}</span>
            </div>
          ) : null}
          {camLog ? <pre className="cam-log">{camLog}</pre> : null}
        </div>
      </div>
    </section>
  );
}

function ExportView({ design, locale }: { design: BoardDesign; locale: Locale }) {
  const easyEdaJson = useMemo(() => JSON.stringify(buildEasyEdaProjectJson(design), null, 2), [design]);
  const bomCsv = useMemo(() => buildBomCsv(design), [design]);
  const reportJson = useMemo(() => buildReportJson(design), [design]);
  const t = (zh: string, en: string) => bilingual(locale, zh, en);

  const downloadText = (filename: string, content: string, type: string) => {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="grid two export-grid">
      <section className="panel">
        <h2>{t("3D 底板预览", "3D board preview")}</h2>
        <Board3D design={design} />
      </section>
      <section className="panel">
        <h2>{t("BOM / 检查 / 导出", "BOM / checks / export")}</h2>
        <div className="summary-strip">
          <span>{t(`${design.bom.length} 模块`, `${design.bom.length} modules`)}</span>
          <span>{t(`$${design.bom.reduce((sum, line) => sum + line.unitPriceUsd * line.qty, 0).toFixed(2)} 估算`, `Estimated $${design.bom.reduce((sum, line) => sum + line.unitPriceUsd * line.qty, 0).toFixed(2)}`)}</span>
          <span>{t(`${design.report.summary.warn} 警告`, `${design.report.summary.warn} warnings`)}</span>
        </div>
        <div className="export-actions">
          <button onClick={() => void downloadExportZip(design)}>{t("下载 JLC 工程 ZIP", "Download JLC project ZIP")}</button>
          <button onClick={() => downloadText("board.easyeda.json", easyEdaJson, "application/json")}>EasyEDA JSON</button>
          <button onClick={() => downloadText("bom.csv", bomCsv, "text/csv")}>BOM CSV</button>
          <button onClick={() => downloadText("report.json", reportJson, "application/json")}>{t("检查报告", "Check report")}</button>
        </div>
        <div className="checks">
          <CheckList title="ERC" items={design.report.erc} />
          <CheckList title="DRC" items={design.report.drc} />
          <CheckList title="DFM" items={design.report.dfm} />
        </div>
      </section>
      <CncToolpathPanel design={design} locale={locale} />
    </div>
  );
}

function PromptView({
  prompt,
  onPrompt,
  onGenerate,
  libraryOpen,
  onCloseLibrary,
  isGenerating,
  locale,
}: {
  prompt: string;
  onPrompt: (value: string) => void;
  onGenerate: () => void | Promise<void>;
  libraryOpen: boolean;
  onCloseLibrary: () => void;
  isGenerating: boolean;
  locale: Locale;
}) {
  const t = (zh: string, en: string) => bilingual(locale, zh, en);
  const activeCase = demoCases.find((item) => matchesCasePrompt(prompt, item)) ?? demoCases[0];
  const selectCase = (item: DemoCase) => {
    onPrompt(locale === "en" ? item.promptEn ?? item.prompt : item.prompt);
    onCloseLibrary();
  };

  if (libraryOpen) {
    return (
      <div className="prompt-home library-mode">
        <section className="panel community-panel library-standalone">
        <div className="section-head">
          <div>
            <p className="eyebrow">{t("公开项目库", "Public project library")}</p>
            <h2>{t("12 个可直接打开的产品项目", "12 ready-to-open product projects")}</h2>
            <p>{t("每个条目都从模块组合、制造输出和机械约束进入同一套工作台。", "Each entry opens the same workbench with module stack, manufacturing output, and mechanical constraints.")}</p>
          </div>
          <button className="library-close" onClick={onCloseLibrary}>
            {t("返回输入", "Back to input")}
          </button>
        </div>
        <div className="community-grid">
          {demoCases.map((item) => {
            const outputs = caseOutputs(item, locale);
            const modules = caseModuleLabels(item);
            return (
              <button
                key={item.id}
                className={matchesCasePrompt(prompt, item) ? "community-card active" : "community-card"}
                onClick={() => selectCase(item)}
              >
                <div className="community-cover">
                  {item.coverImage ? <img src={item.coverImage} alt={locale === "en" ? item.nameEn ?? item.name : item.name} loading="lazy" /> : null}
                  <div className="community-device-sheen" />
                  <div className="community-cover-shadow" />
                  <span className="community-cover-badge">{caseIndustry(item, locale)}</span>
                  <span className="community-status-badge">{caseStatus(item, locale)}</span>
                </div>
                <div className="community-card-body">
                  <div className="community-card-head">
                    <strong>{locale === "en" ? item.nameEn ?? item.name : item.name}</strong>
                    <span>{item.preferredModules.length} modules</span>
                  </div>
                  <p>{caseSummary(item, locale)}</p>
                  <div className="community-module-row">
                    {modules.map((label) => (
                      <span key={`${item.id}-${label}`}>{label}</span>
                    ))}
                  </div>
                  <div className="community-meta-row">
                    <span>{item.boardSizeMm.w} x {item.boardSizeMm.h} mm</span>
                    <span>{outputs.slice(0, 3).join(" / ")}</span>
                  </div>
                  <div className="community-tags">
                    {item.intentTags.slice(0, 3).map((tag) => (
                      <span key={`${item.id}-${tag}`}>{tag}</span>
                    ))}
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      </section>
      </div>
    );
  }

  return (
    <div className="prompt-home prompt-only">
      <div className="grid two prompt-grid">
      <section className="panel">
        <h2>{t("自然语言需求", "Natural language brief")}</h2>
        <textarea value={prompt} onChange={(event) => onPrompt(event.target.value)} />
        <div className="actions">
          <button className="primary" onClick={() => void onGenerate()} disabled={isGenerating}>
            {isGenerating ? t("正在生成...", "Generating...") : t("生成模块底板", "Generate board")}
          </button>
        </div>
      </section>
      <section className="panel">
        <div className="panel-head">
          <div>
            <p className="eyebrow">{t("当前项目", "Active project")}</p>
            <h2>{locale === "en" ? activeCase.nameEn ?? activeCase.name : activeCase.name}</h2>
          </div>
        </div>
        <div className="active-project-card">
          {activeCase.coverImage ? <img src={activeCase.coverImage} alt={locale === "en" ? activeCase.nameEn ?? activeCase.name : activeCase.name} /> : null}
          <p>{caseSummary(activeCase, locale)}</p>
          <div className="active-project-metrics">
            <span>{caseIndustry(activeCase, locale)}</span>
            <span>{caseStatus(activeCase, locale)}</span>
            <span>{caseOutputs(activeCase, locale).join(" / ")}</span>
          </div>
          <div className="community-tags">
            {activeCase.intentTags.map((tag) => (
              <span key={`${activeCase.id}-${tag}`}>{tag}</span>
            ))}
          </div>
          <div className="summary-strip">
            <span>{activeCase.boardSizeMm.w} x {activeCase.boardSizeMm.h} mm</span>
            <span>{activeCase.preferredModules.length} modules</span>
          </div>
        </div>
      </section>
      </div>
    </div>
  );
}

export default function App() {
  const [locale, setLocale] = useState<Locale>(() => {
    if (typeof window === "undefined") return "en";
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
    return stored === "zh" ? "zh" : "en";
  });
  const initialPrompt = locale === "en" ? defaultPromptEn : defaultPrompt;
  const [prompt, setPrompt] = useState(initialPrompt);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(demoCases[0].id);
  const [active, setActive] = useState<Step>("Prompt");
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [design, setDesign] = useState(() => generateDesign(initialPrompt, locale));
  const [generationKey, setGenerationKey] = useState(0);
  const [generationRun, setGenerationRun] = useState<GenerationRunState>({ active: false, phaseIndex: 0, progress: 0 });
  const generationInFlightRef = useRef(false);
  const t = (zh: string, en: string) => bilingual(locale, zh, en);
  const bomTotal = useMemo(
    () => design.bom.reduce((sum, line) => sum + line.unitPriceUsd * line.qty, 0),
    [design.bom],
  );
  const projectCase = caseById(selectedCaseId ?? design.id);
  const projectTitle = caseTitle(projectCase, locale) || design.title;
  const stageTitle = active === "Prompt" && libraryOpen ? t("公开项目库", "Public project library") : stepLabel(locale, active);

  useEffect(() => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  }, [locale]);

  useEffect(() => {
    const currentCase = caseById(selectedCaseId ?? design.id);
    if (!matchesCasePrompt(prompt, currentCase)) return;
    const nextPrompt = casePrompt(currentCase, locale);
    if (nextPrompt !== prompt) {
      setPrompt(nextPrompt);
    }
  }, [design.id, locale, prompt, selectedCaseId]);

  const handlePromptChange = (value: string) => {
    setPrompt(value);
    const matched = demoCases.find((item) => matchesCasePrompt(value, item));
    setSelectedCaseId(matched?.id ?? null);
  };

  const handleStepChange = (next: Step) => {
    setActive(next);
    if (next !== "Prompt") {
      setLibraryOpen(false);
    }
  };

  const openProjectLibrary = () => {
    setActive("Prompt");
    setLibraryOpen(true);
  };

  const runGenerate = async () => {
    if (generationInFlightRef.current) return;
    generationInFlightRef.current = true;
    const requestPrompt = prompt;
    const requestLocale = locale;
    const lastPhaseIndex = generationPhases.length - 1;

    try {
      for (let index = 0; index < generationPhases.length; index += 1) {
        const phaseStart = Math.max(6, Math.round((index / generationPhases.length) * 100) + 6);
        const phaseEnd = Math.min(96, Math.round(((index + 1) / generationPhases.length) * 100) - (index === lastPhaseIndex ? 4 : 2));
        setGenerationRun({ active: true, phaseIndex: index, progress: phaseStart });
        await wait(260);
        setGenerationRun({ active: true, phaseIndex: index, progress: phaseEnd });
        await wait(index === lastPhaseIndex ? 220 : 260);
      }

      const nextDesign = generateDesign(requestPrompt, requestLocale);
      setGenerationRun({ active: true, phaseIndex: lastPhaseIndex, progress: 100 });
      await wait(180);
      setDesign(nextDesign);
      setSelectedCaseId(nextDesign.id);
      setGenerationKey((current) => current + 1);
      setLibraryOpen(false);
      setActive("Generate");
    } finally {
      generationInFlightRef.current = false;
      setGenerationRun({ active: false, phaseIndex: 0, progress: 0 });
    }
  };

  const updatePlacement = useCallback((moduleId: string, x: number, y: number) => {
    setDesign((current) => {
      const target = moduleById(moduleId);
      let changed = false;
      const placements = current.placements.map((placement) =>
        placement.moduleId === moduleId
          ? (() => {
              const nextPlacement = clampPlacement(target, { ...placement, x, y }, current.boardSizeMm);
              if (nextPlacement.x !== placement.x || nextPlacement.y !== placement.y) changed = true;
              return nextPlacement;
            })()
          : placement,
      );

      if (!changed) return current;

      const tracks = routeNets(current.nets, placements, current.boardSizeMm, current.keepouts);
      const nextDesign = {
        ...current,
        placements,
        tracks,
      };
      return { ...nextDesign, report: checkDesign(nextDesign, locale) };
    });
  }, [locale]);

  const rotateModule = useCallback((moduleId: string) => {
    setDesign((current) => {
      const target = moduleById(moduleId);
      const placements = current.placements.map((placement) => {
        if (placement.moduleId !== moduleId) return placement;
        const nextRotation = ((placement.rotation + 90) % 360) as Placement["rotation"];
        return clampPlacement(target, { ...placement, rotation: nextRotation }, current.boardSizeMm);
      });

      const tracks = routeNets(current.nets, placements, current.boardSizeMm, current.keepouts);
      const nextDesign = {
        ...current,
        placements,
        tracks,
      };
      return { ...nextDesign, report: checkDesign(nextDesign, locale) };
    });
  }, [locale]);

  const rerouteSketch = useCallback(() => {
    setDesign((current) => {
      const nextDesign = {
        ...current,
        tracks: routeNets(current.nets, current.placements, current.boardSizeMm, current.keepouts),
      };
      return { ...nextDesign, report: checkDesign(nextDesign, locale) };
    });
  }, [locale]);

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Blockless PCB Studio</p>
          <h1>{t("模块化 PCB 设计工作台", "Modular PCB design workstation")}</h1>
        </div>
        <div className="header-tools">
          <div className="header-actions">
            <button className={libraryOpen ? "library-launch active" : "library-launch"} onClick={openProjectLibrary}>
              {t("公开项目库", "Public library")}
            </button>
            <div className="segmented-control locale-toggle" aria-label={t("语言切换", "Language switch")}>
              <button className={locale === "zh" ? "active" : ""} onClick={() => setLocale("zh")}>
                中文
              </button>
              <button className={locale === "en" ? "active" : ""} onClick={() => setLocale("en")}>
                EN
              </button>
            </div>
          </div>
          <div className="header-stats">
            <span>{t("模块选型", "Module selection")}</span>
            <span>{t("JLC EDA 导出", "JLC EDA export")}</span>
            <span>{t("自动布局布线", "Auto placement & routing")}</span>
            <span>{t("STEP/STL 3D 预览", "STEP/STL 3D preview")}</span>
            <span>{t("CAM/CNC 刀路", "CAM/CNC toolpaths")}</span>
          </div>
        </div>
      </header>

      <div className="workspace-layout">
        <aside className="workspace-rail">
          <section className="rail-card">
            <p className="eyebrow">{t("项目", "Project")}</p>
            <strong>{projectTitle}</strong>
            <div className="summary-strip">
              <span>
                {design.boardSizeMm.w} x {design.boardSizeMm.h} mm
              </span>
              <span>{t(`${design.selectedModules.length} 个模块`, `${design.selectedModules.length} modules`)}</span>
            </div>
          </section>
          <section className="rail-card">
            <p className="eyebrow">{t("工作区", "Workspace")}</p>
            <Stepper active={active} onChange={handleStepChange} locale={locale} />
          </section>
          <section className="rail-card">
            <p className="eyebrow">{t("状态", "Status")}</p>
            <div className="summary-strip">
              <span>{t(`${design.report.summary.pass} 通过`, `${design.report.summary.pass} pass`)}</span>
              <span>{t(`${design.report.summary.warn} 警告`, `${design.report.summary.warn} warn`)}</span>
              <span>{t(`${design.report.summary.fail} 失败`, `${design.report.summary.fail} fail`)}</span>
              <span>${bomTotal.toFixed(2)}</span>
            </div>
          </section>
        </aside>

        <section className="workspace-stage">
          <div className="stage-head">
            <div>
              <p className="eyebrow">{t("当前视图", "Active view")}</p>
              <h2>{stageTitle}</h2>
            </div>
            <div className="stage-metrics">
              <span>{t(`${design.selectedModules.length} 个模块`, `${design.selectedModules.length} modules`)}</span>
              <span>{t(`${design.nets.length} 条网络`, `${design.nets.length} nets`)}</span>
              <span>
                {design.boardSizeMm.w} x {design.boardSizeMm.h} mm
              </span>
              <span>{t(`${design.report.summary.warn} 警告`, `${design.report.summary.warn} warn`)}</span>
            </div>
          </div>

          <div className="workspace-view">
            {active === "Prompt" && (
              <PromptView
                prompt={prompt}
                onPrompt={handlePromptChange}
                onGenerate={runGenerate}
                libraryOpen={libraryOpen}
                onCloseLibrary={() => setLibraryOpen(false)}
                isGenerating={generationRun.active}
                locale={locale}
              />
            )}
            {active === "Plan" && <PlanView design={design} locale={locale} />}
            {active === "Review" && <ReviewView design={design} locale={locale} />}
            {active === "Generate" && (
              <GenerateView
                design={design}
                generationKey={generationKey}
                onPlacementChange={updatePlacement}
                onRotateModule={rotateModule}
                onRerouteSketch={rerouteSketch}
                locale={locale}
              />
            )}
            {active === "Export" && <ExportView design={design} locale={locale} />}
            <GenerationProgressOverlay run={generationRun} locale={locale} />
          </div>
        </section>
      </div>

      <footer>
        <span>{t("工作流：商用模块库 + 接口匹配 + KiCad/KRT 布线校验", "Workflow: commercial module library + interface matching + KiCad/KRT routing checks")}</span>
        <span>{t("输出：JLC EDA 工程包、BOM、检查报告、3D 预览和 CAM/CNC 刀路", "Output: JLC EDA package, BOM, check report, 3D preview, and CAM/CNC toolpaths")}</span>
      </footer>
    </main>
  );
}
