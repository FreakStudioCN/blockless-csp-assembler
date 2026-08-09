import type { BoardDesign, HardwareModule, Placement } from "../domain/types";
import { rotatedSize } from "./geometry";

export interface EnclosureMountingHole {
  id: string;
  x: number;
  y: number;
  diameterMm: number;
}

export interface EnclosureSettings {
  wallMm: number;
  clearanceMm: number;
  baseHeightMm: number;
  floorMm: number;
  lidThicknessMm: number;
  standoffHeightMm: number;
  cornerRadiusMm: number;
}

export type EnclosureTopCutoutKind = "sensor-vent" | "display-window" | "antenna-window";
export type EnclosureSideCutoutKind = "usb-port" | "connector-access" | "antenna-access";
export type EnclosureEdge = "front" | "back" | "left" | "right";

export interface EnclosureTopCutout {
  id: string;
  kind: EnclosureTopCutoutKind;
  moduleId: string;
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface EnclosureSideCutout {
  id: string;
  kind: EnclosureSideCutoutKind;
  edge: EnclosureEdge;
  moduleId: string;
  label: string;
  centerMm: number;
  widthMm: number;
  heightMm: number;
  bottomMm: number;
}

export interface EnclosureStandoff {
  id: string;
  x: number;
  y: number;
  holeDiameterMm: number;
  postDiameterMm: number;
  heightMm: number;
}

export interface EnclosureSpec {
  boardSize: { w: number; h: number };
  innerSize: { w: number; h: number };
  outerSize: { w: number; h: number; d: number };
  settings: EnclosureSettings;
  standoffs: EnclosureStandoff[];
  topCutouts: EnclosureTopCutout[];
  sideCutouts: EnclosureSideCutout[];
  notes: string[];
}

export const defaultEnclosureSettings: EnclosureSettings = {
  wallMm: 2,
  clearanceMm: 3,
  baseHeightMm: 17,
  floorMm: 1.6,
  lidThicknessMm: 2.2,
  standoffHeightMm: 4.2,
  cornerRadiusMm: 2.5,
};

function wordsFor(module: HardwareModule) {
  return [
    module.id,
    module.name,
    module.description,
    module.connector,
    module.category,
    ...module.reasonTags,
    ...module.interfaces,
  ]
    .join(" ")
    .toLowerCase();
}

function nearestEdge(placement: Placement, boardSize: { w: number; h: number }): EnclosureEdge {
  const candidates: Array<{ edge: EnclosureEdge; distance: number }> = [
    { edge: "left", distance: placement.x },
    { edge: "right", distance: boardSize.w - placement.x },
    { edge: "front", distance: placement.y },
    { edge: "back", distance: boardSize.h - placement.y },
  ];
  return candidates.sort((a, b) => a.distance - b.distance)[0].edge;
}

function sideCenterFor(edge: EnclosureEdge, placement: Placement) {
  return edge === "front" || edge === "back" ? placement.x : placement.y;
}

function topExposureFor(module: HardwareModule, placement: Placement): Pick<EnclosureTopCutout, "kind" | "label" | "w" | "h"> | null {
  const size = rotatedSize(module, placement);
  const words = wordsFor(module);
  if (module.category === "display") {
    return {
      kind: "display-window",
      label: `${module.model3d.label} display window`,
      w: Math.min(size.w + 4, 34),
      h: Math.min(size.h * 0.62 + 4, 22),
    };
  }
  if (module.category === "sensor" || words.includes("air_quality") || words.includes("environment")) {
    return {
      kind: "sensor-vent",
      label: `${module.model3d.label} sensor vent`,
      w: Math.min(size.w + 5, 26),
      h: Math.min(size.h + 5, 22),
    };
  }
  if (module.category === "wireless" || words.includes("antenna") || words.includes("lora")) {
    return {
      kind: "antenna-window",
      label: `${module.model3d.label} RF clearance window`,
      w: Math.min(size.w + 5, 32),
      h: Math.min(size.h * 0.36 + 4, 16),
    };
  }
  return null;
}

function sideOpeningFor(module: HardwareModule, placement: Placement): Pick<EnclosureSideCutout, "kind" | "label" | "widthMm" | "heightMm" | "bottomMm"> | null {
  const size = rotatedSize(module, placement);
  const words = wordsFor(module);
  if (words.includes("usb")) {
    return {
      kind: "usb-port",
      label: `${module.model3d.label} USB access`,
      widthMm: Math.max(10, Math.min(size.w + 4, 18)),
      heightMm: 6,
      bottomMm: 6.2,
    };
  }
  if (module.category === "connector" || words.includes("jst") || words.includes("qwiic") || words.includes("grove")) {
    return {
      kind: "connector-access",
      label: `${module.model3d.label} connector access`,
      widthMm: Math.max(9, Math.min(size.w + 4, 18)),
      heightMm: 5.2,
      bottomMm: 6,
    };
  }
  if (words.includes("antenna") || words.includes("lora")) {
    return {
      kind: "antenna-access",
      label: `${module.model3d.label} antenna access`,
      widthMm: Math.max(12, Math.min(size.w + 6, 24)),
      heightMm: 4.8,
      bottomMm: 8,
    };
  }
  return null;
}

export function buildEnclosureSpec(
  design: BoardDesign,
  mountingHoles: EnclosureMountingHole[],
  settings: EnclosureSettings = defaultEnclosureSettings,
): EnclosureSpec {
  const innerSize = {
    w: design.boardSizeMm.w + settings.clearanceMm * 2,
    h: design.boardSizeMm.h + settings.clearanceMm * 2,
  };
  const outerSize = {
    w: innerSize.w + settings.wallMm * 2,
    h: innerSize.h + settings.wallMm * 2,
    d: settings.floorMm + settings.baseHeightMm + settings.lidThicknessMm,
  };
  const standoffs = mountingHoles.map((hole) => ({
    id: hole.id,
    x: hole.x,
    y: hole.y,
    holeDiameterMm: hole.diameterMm,
    postDiameterMm: Math.max(hole.diameterMm + 3.6, 6.6),
    heightMm: settings.standoffHeightMm,
  }));
  const topCutouts: EnclosureTopCutout[] = [];
  const sideCutouts: EnclosureSideCutout[] = [];

  for (const placement of design.placements) {
    const module = design.selectedModules.find((item) => item.module.id === placement.moduleId)?.module;
    if (!module) continue;

    const exposure = topExposureFor(module, placement);
    if (exposure) {
      topCutouts.push({
        id: `top-${placement.moduleId}`,
        moduleId: placement.moduleId,
        x: Number(placement.x.toFixed(2)),
        y: Number(placement.y.toFixed(2)),
        ...exposure,
      });
    }

    const opening = sideOpeningFor(module, placement);
    if (opening) {
      const edge = nearestEdge(placement, design.boardSizeMm);
      sideCutouts.push({
        id: `side-${placement.moduleId}`,
        moduleId: placement.moduleId,
        edge,
        centerMm: Number(sideCenterFor(edge, placement).toFixed(2)),
        ...opening,
      });
    }
  }

  const notes = [
    `${standoffs.length} PCB mounting holes mapped to enclosure standoffs`,
    `${topCutouts.length} top exposure cutouts inferred from sensors, display, and RF modules`,
    `${sideCutouts.length} side access openings inferred from USB, connector, and antenna modules`,
  ];

  return {
    boardSize: design.boardSizeMm,
    innerSize,
    outerSize,
    settings,
    standoffs,
    topCutouts,
    sideCutouts,
    notes,
  };
}

function n(value: number) {
  return Number(value.toFixed(3));
}

function boardX(spec: EnclosureSpec, x: number) {
  return n(spec.settings.wallMm + spec.settings.clearanceMm + x);
}

function boardY(spec: EnclosureSpec, y: number) {
  return n(spec.settings.wallMm + spec.settings.clearanceMm + y);
}

function sideCutoutScad(spec: EnclosureSpec, cutout: EnclosureSideCutout) {
  const wall = spec.settings.wallMm;
  const z = cutout.bottomMm;
  const width = cutout.widthMm;
  const h = cutout.heightMm;
  if (cutout.edge === "front") {
    return `  translate([${boardX(spec, cutout.centerMm - width / 2)}, -0.2, ${n(z)}]) cube([${n(width)}, ${n(wall + 0.4)}, ${n(h)}]); // ${cutout.label}`;
  }
  if (cutout.edge === "back") {
    return `  translate([${boardX(spec, cutout.centerMm - width / 2)}, ${n(spec.outerSize.h - wall - 0.2)}, ${n(z)}]) cube([${n(width)}, ${n(wall + 0.4)}, ${n(h)}]); // ${cutout.label}`;
  }
  if (cutout.edge === "left") {
    return `  translate([-0.2, ${boardY(spec, cutout.centerMm - width / 2)}, ${n(z)}]) cube([${n(wall + 0.4)}, ${n(width)}, ${n(h)}]); // ${cutout.label}`;
  }
  return `  translate([${n(spec.outerSize.w - wall - 0.2)}, ${boardY(spec, cutout.centerMm - width / 2)}, ${n(z)}]) cube([${n(wall + 0.4)}, ${n(width)}, ${n(h)}]); // ${cutout.label}`;
}

export function buildEnclosureOpenScad(spec: EnclosureSpec) {
  const s = spec.settings;
  const boardLift = s.floorMm + s.standoffHeightMm;
  const bodyHeight = s.floorMm + s.baseHeightMm;
  const topCutouts = spec.topCutouts
    .map(
      (cutout) =>
        `  translate([${boardX(spec, cutout.x - cutout.w / 2)}, ${boardY(spec, cutout.y - cutout.h / 2)}, -0.1]) cube([${n(cutout.w)}, ${n(cutout.h)}, ${n(s.lidThicknessMm + 0.2)}]); // ${cutout.label}`,
    )
    .join("\n");
  const sideCutouts = spec.sideCutouts.map((cutout) => sideCutoutScad(spec, cutout)).join("\n");
  const posts = spec.standoffs
    .map(
      (post) => `  translate([${boardX(spec, post.x)}, ${boardY(spec, post.y)}, ${n(s.floorMm)}])
    difference() {
      cylinder(h=${n(post.heightMm)}, d=${n(post.postDiameterMm)});
      translate([0, 0, -0.1]) cylinder(h=${n(post.heightMm + 0.2)}, d=${n(post.holeDiameterMm)});
    }`,
    )
    .join("\n");
  const lidHoles = spec.standoffs
    .map(
      (post) =>
        `  translate([${boardX(spec, post.x)}, ${boardY(spec, post.y)}, -0.1]) cylinder(h=${n(s.lidThicknessMm + 0.2)}, d=${n(post.holeDiameterMm + 0.3)});`,
    )
    .join("\n");

  return [
    "// Generated by Blockless PCB Studio",
    "// Parametric rectangular enclosure, units: mm",
    "$fn = 48;",
    `outer_w = ${n(spec.outerSize.w)};`,
    `outer_d = ${n(spec.outerSize.h)};`,
    `body_h = ${n(bodyHeight)};`,
    `lid_t = ${n(s.lidThicknessMm)};`,
    "",
    "module bottom_shell() {",
    "  union() {",
    "    difference() {",
    "      cube([outer_w, outer_d, body_h]);",
    `      translate([${n(s.wallMm)}, ${n(s.wallMm)}, ${n(s.floorMm)}]) cube([${n(spec.innerSize.w)}, ${n(spec.innerSize.h)}, ${n(s.baseHeightMm + 0.4)}]);`,
    sideCutouts || "      // no side cutouts inferred",
    "    }",
    posts || "    // no standoffs",
    "  }",
    "}",
    "",
    "module lid() {",
    "  difference() {",
    "    cube([outer_w, outer_d, lid_t]);",
    topCutouts || "    // no top cutouts inferred",
    lidHoles || "    // no lid screw holes",
    "  }",
    "}",
    "",
    "bottom_shell();",
    `translate([0, outer_d + 12, 0]) lid();`,
    "",
    `// PCB origin inside enclosure: x=${n(s.wallMm + s.clearanceMm)}, y=${n(s.wallMm + s.clearanceMm)}, z=${n(boardLift)}`,
  ].join("\n");
}
