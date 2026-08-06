import { useMemo, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { useEffect, useRef } from "react";
import { demoCases, defaultPrompt } from "./data/caseRegistry";
import { moduleLibrary } from "./data/modules";
import { moduleMarketplace } from "./data/marketplace";
import { generateDesign } from "./engine/generate";
import { buildHarvestProfiles } from "./engine/harvester";
import { clampPlacement, getPinPoint, pcbToScene } from "./engine/geometry";
import { routeNets } from "./engine/router";
import { checkDesign } from "./engine/checker";
import { buildBomCsv, buildEasyEdaProjectJson, buildReportJson, downloadExportZip } from "./engine/exporter";
import type { BoardDesign, CheckItem, HardwareModule, Placement, TrackSegment } from "./domain/types";

const steps = ["Prompt", "Plan", "Review", "Generate", "Export"] as const;
type Step = (typeof steps)[number];
const stepLabels: Record<Step, string> = {
  Prompt: "输入",
  Plan: "选型",
  Review: "检查",
  Generate: "布局",
  Export: "导出",
};

const trackColors: Record<TrackSegment["kind"], string> = {
  power: "#d24b3a",
  gnd: "#20252b",
  i2c: "#2368c4",
  uart: "#229862",
  gpio: "#8064c9",
  spi: "#b76b1c",
};

type BoardLayer = "top" | "bottom";
type KicadPreviewTab = BoardLayer | "appearance";
type SketchView = BoardLayer | "three";
type RouteRunStatus = "idle" | "running" | "ok" | "error";

type MountingHole = {
  id: string;
  x: number;
  y: number;
  diameterMm: number;
};

const GRID_SNAP_MM = 2.54;

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

function sourceImageFor(module: HardwareModule) {
  const listing = moduleMarketplace[module.id]?.amazon;
  return listing?.imageUrl || module.model3d.imageUrl || "";
}

function SourceCard({ module }: { module: HardwareModule }) {
  const amazon = moduleMarketplace[module.id]?.amazon;
  const lcsc = moduleMarketplace[module.id]?.lcsc;
  const imageUrl = sourceImageFor(module);

  return (
    <article className="source-card">
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
          <span>{amazon?.hint ?? "Amazon"}</span>
          <span>{lcsc?.hint ?? "LCSC"}</span>
        </div>
        <div className="source-card-actions">
          {amazon ? (
            <a href={amazon.url} target="_blank" rel="noreferrer" className="source-button primary">
              Amazon
            </a>
          ) : null}
          {lcsc ? (
            <a href={lcsc.url} target="_blank" rel="noreferrer" className="source-button">
              LCSC
            </a>
          ) : (
            <a href={module.purchaseUrl} target="_blank" rel="noreferrer" className="source-button">
              Purchase
            </a>
          )}
        </div>
      </div>
    </article>
  );
}

function statusLabel(status: HardwareModule["reviewStatus"]) {
  if (status === "verified") return "已审阅";
  if (status === "needs_review") return "需复核";
  return "参数化";
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
    onPlacementChange(drag.moduleId, pointer.x - drag.dx, pointer.y - drag.dy);
  };

  const onSvgPointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    const target = event.target as Element;
    if (target.closest(".draggable-module")) return;
    setBoardClickStart(pointerToBoard(event));
  };

  const onSvgPointerUp = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (drag) {
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
                {track.points.slice(1, -1).map((point, viaIndex) => (
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
                {track.points.slice(1, -1).map((point, viaIndex) => (
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
              onSelectModule?.(item.id);
              if (!onPlacementChange || isLocked) return;
              const pointer = pointerToBoard(event as unknown as ReactPointerEvent<SVGSVGElement>);
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
  onBoardClick?: (x: number, y: number) => void;
  mountingHoles?: MountingHole[];
  lockedModuleIds?: string[];
  showRoutes?: boolean;
  routeStyle?: "normal" | "pseudo";
  holePlacementEnabled?: boolean;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const host = ref.current;
    if (!host) return;

    const width = host.clientWidth;
    const height = Math.max(320, host.clientHeight);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#f7faf8");

    const camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 1000);
    camera.position.set(0, 62, 86);
    camera.lookAt(0, 0, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    host.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 45;
    controls.maxDistance = 145;
    controls.maxPolarAngle = Math.PI * 0.47;
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
      }
    };

    const drawRoutes = (placements: Placement[]) => {
      clearRouteRoot();
      if (!showRoutes) return;
      const tracks =
        routeStyle === "pseudo"
          ? routeNets(design.nets, placements, design.boardSizeMm, design.keepouts)
          : design.tracks;
      for (const track of tracks) {
        const y = track.layer === "top" ? copperY : -0.15;
        routeRoot.add(makeTrackMesh(track.points, design.boardSizeMm, scale, track.widthMm, trackColors[track.kind], y, routeStyle));
        for (const point of track.points.slice(1, -1)) {
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

    group.add(routeRoot);
    drawRoutes(design.placements);

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
    const moduleRoots: Array<{ id: string; root: THREE.Group }> = [];

    for (const placement of design.placements) {
      const item = moduleById(placement.moduleId);
      const moduleW = item.sizeMm.w * scale;
      const moduleH = item.sizeMm.h * scale;
      const moduleHeight = item.category === "connector" ? 3.2 : item.category === "power" ? 4 : 5;
      const hasRealStl = item.model3d.mode === "stl" && Boolean(item.model3d.localUrl);
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
          opacity: hasRealStl ? 0.16 : 1,
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

      const detailTopY = hasRealStl
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
        onSelectModule?.(moduleRoot.userData.moduleId);
        if (!onPlacementChange || moduleRoot.userData.locked) return;
        controls.enabled = false;
        renderer.domElement.setPointerCapture(event.pointerId);
        activeDrag = {
          moduleId: moduleRoot.userData.moduleId,
          root: moduleRoot,
          offset: hitPoint.clone().sub(moduleRoot.position),
        };
        return;
      }

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
      const boardPoint = sceneToBoard(next);
      const livePlacements = design.placements.map((placement) =>
        placement.moduleId === activeDrag?.moduleId
          ? { ...placement, x: boardPoint.x, y: boardPoint.y }
          : placement,
      );
      drawRoutes(livePlacements);
    };

    const onPointerUp = (event: PointerEvent) => {
      if (activeDrag) {
        renderer.domElement.releasePointerCapture(event.pointerId);
        controls.enabled = true;
        const boardX = activeDrag.root.position.x / scale + design.boardSizeMm.w / 2;
        const boardY = activeDrag.root.position.z / scale + design.boardSizeMm.h / 2;
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
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      renderer.domElement.removeEventListener("pointerdown", onPointerDown);
      renderer.domElement.removeEventListener("pointermove", onPointerMove);
      renderer.domElement.removeEventListener("pointerup", onPointerUp);
      renderer.domElement.removeEventListener("pointercancel", onPointerUp);
      controls.dispose();
      renderer.dispose();
      host.removeChild(renderer.domElement);
    };
  }, [design, holePlacementEnabled, lockedModuleIds, mountingHoles, onBoardClick, onPlacementChange, onSelectModule, routeStyle, showRoutes]);

  return <div className={`three-host ${holePlacementEnabled ? "hole-mode" : ""}`} ref={ref} />;
}

function Stepper({ active, onChange }: { active: Step; onChange: (step: Step) => void }) {
  return (
    <div className="stepper">
      {steps.map((step) => (
        <button
          key={step}
          className={active === step ? "step active" : "step"}
          onClick={() => onChange(step)}
          aria-current={active === step ? "page" : undefined}
        >
          {stepLabels[step]}
        </button>
      ))}
    </div>
  );
}

function PlanView({ design }: { design: BoardDesign }) {
  return (
    <div className="grid two">
      <section className="panel">
        <h2>AI模块方案</h2>
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
        <h2>接口匹配</h2>
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

function ReviewView({ design }: { design: BoardDesign }) {
  const harvestProfiles = buildHarvestProfiles(design.selectedModules);
  const averageConfidence = Math.round(
    harvestProfiles.reduce((sum, profile) => sum + profile.confidence, 0) / harvestProfiles.length,
  );

  return (
    <section className="panel">
      <div className="section-head">
        <div>
          <h2>模块资料抓取 / 审阅</h2>
          <p>首版走缓存优先：自动补全尺寸、接口、Pin map、采购链接和参数化3D模型；人工只复核低置信项。</p>
        </div>
        <div className="confidence-meter">
          <strong>{averageConfidence}%</strong>
          <span>平均资料置信度</span>
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
              <th>模块</th>
              <th>尺寸</th>
              <th>接口</th>
              <th>电压</th>
              <th>3D状态</th>
              <th>审阅</th>
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
                  <strong>{module.model3d.mode === "stl" ? "真实STL" : "参数化/STEP"}</strong>
                  <small>{module.model3d.localUrl ?? module.model3d.localStepUrl ?? "等待抓取"}</small>
                </td>
                <td>
                  <span className={`badge ${module.reviewStatus}`}>{statusLabel(module.reviewStatus)}</span>
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

function GenerateMotionOverlay({ design, phase }: { design: BoardDesign; phase: AssemblyPhase }) {
  if (phase === "idle") return null;

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
        <strong>{phase === "placing" ? "模块自动装配" : "线路动态生成"}</strong>
        <span>{phase === "placing" ? "按约束把模块落到 PCB 底板" : "根据当前布局重新生成顶层/底层草图布线"}</span>
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
}: {
  design: BoardDesign;
  generationKey: number;
  onPlacementChange: (moduleId: string, x: number, y: number) => void;
  onRotateModule: (moduleId: string) => void;
  onRerouteSketch: () => void;
}) {
  const [previewMode, setPreviewMode] = useState<"kicad" | "interactive">("kicad");
  const [kicadTab, setKicadTab] = useState<KicadPreviewTab>("top");
  const [sketchView, setSketchView] = useState<SketchView>("top");
  const [selectedModuleId, setSelectedModuleId] = useState(design.placements[0]?.moduleId ?? "");
  const [lockedModuleIds, setLockedModuleIds] = useState<string[]>([]);
  const [snapToGrid, setSnapToGrid] = useState(true);
  const [holePlacementEnabled, setHolePlacementEnabled] = useState(false);
  const [holeDiameterMm, setHoleDiameterMm] = useState(3.2);
  const [mountingHoles, setMountingHoles] = useState<MountingHole[]>(() => defaultMountingHoles(design.boardSizeMm));
  const [assetVersion, setAssetVersion] = useState(() => Date.now());
  const [routeRun, setRouteRun] = useState<{ status: RouteRunStatus; message: string }>({
    status: "idle",
    message: "本机 KiCad/KRT 就绪后可重新生成真实 PCB 布线。",
  });
  const [routingRevision, setRoutingRevision] = useState(0);
  const [assemblyPhase, setAssemblyPhase] = useState<AssemblyPhase>("idle");
  const kicadRender = kicadRenderMeta[design.id];
  const selectedPlacement = design.placements.find((placement) => placement.moduleId === selectedModuleId);
  const selectedModule = selectedPlacement ? moduleById(selectedPlacement.moduleId) : undefined;
  const mechanicalChecks = design.report.drc.filter((item) => item.code.startsWith("DRC-MECH"));

  useEffect(() => {
    setSelectedModuleId(design.placements[0]?.moduleId ?? "");
    setLockedModuleIds([]);
    setMountingHoles(defaultMountingHoles(design.boardSizeMm));
    setHoleDiameterMm(3.2);
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

  const withAssetVersion = (url: string) => `${url}?v=${assetVersion}`;

  const handlePlacementChange = (moduleId: string, x: number, y: number) => {
    if (lockedModuleIds.includes(moduleId)) return;
    setSelectedModuleId(moduleId);
    setRoutingRevision((current) => current + 1);
    onPlacementChange(moduleId, snapToGrid ? snapMm(x) : x, snapToGrid ? snapMm(y) : y);
  };

  const addMountingHole = (x: number, y: number) => {
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
  };

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
    setRouteRun({ status: "running", message: "正在调用本机 KiCad/KRT，生成顶层/底层 SVG 和 3D PNG..." });
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
        setRouteRun({ status: "ok", message: "KiCad/KRT 已重新生成真实 PCB、顶层/底层 SVG 和 3D 渲染图。" });
        return;
      }

      for (let attempt = 0; attempt < 240; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
        const statusResponse = await fetch(`/api/kicad/status?t=${Date.now()}`);
        const contentType = statusResponse.headers.get("content-type") ?? "";
        if (!contentType.includes("application/json")) {
          setRouteRun({ status: "idle", message: "本机路由已启动；当前 dev server 需重启后才支持状态轮询。" });
          return;
        }
        const statusResult = (await statusResponse.json().catch(() => ({}))) as {
          state?: { status?: RouteRunStatus; output?: string };
        };
        const status = statusResult.state?.status;
        if (status === "ok") {
          setAssetVersion(Date.now());
          setRouteRun({ status: "ok", message: "KiCad/KRT 已重新生成真实 PCB、顶层/底层 SVG 和 3D 渲染图。" });
          return;
        }
        if (status === "error") {
          throw new Error(statusResult.state?.output ?? "KiCad/KRT routing failed.");
        }
      }

      setRouteRun({ status: "running", message: "KiCad/KRT 仍在后台运行；完成后刷新 KiCad真实输出即可看到新图。" });
    } catch (error) {
      const message = error instanceof Error ? error.message : "重新生成失败。";
      setRouteRun({ status: "error", message: message.slice(0, 260) });
    }
  };

  return (
    <div className="generate-workspace">
      <div className="generate-summary">
        <span>{design.selectedModules.length} modules</span>
        <span>{design.nets.length} nets</span>
        <span>{design.bom.length} BOM lines</span>
        <span>${design.bom.reduce((sum, line) => sum + line.unitPriceUsd * line.qty, 0).toFixed(2)}</span>
      </div>
      <section className="panel visual-panel">
        <div className="panel-head">
          <h2>KiCad真实输出 / 可交互草图</h2>
          <div className="segmented-control" aria-label="预览模式">
            <button className={previewMode === "kicad" ? "active" : ""} onClick={() => setPreviewMode("kicad")}>
              KiCad真实输出
            </button>
            <button
              className={previewMode === "interactive" ? "active" : ""}
              onClick={() => setPreviewMode("interactive")}
            >
              可拖拽草图
            </button>
          </div>
        </div>
        <div className="visual-stage">
          {previewMode === "kicad" && kicadRender ? (
          <div className="kicad-preview">
            <div className="layer-tabs" aria-label="KiCad真实输出层">
              <button className={kicadTab === "top" ? "active" : ""} onClick={() => setKicadTab("top")}>
                顶层 F.Cu
              </button>
              <button className={kicadTab === "bottom" ? "active" : ""} onClick={() => setKicadTab("bottom")}>
                底层 B.Cu
              </button>
              <button className={kicadTab === "appearance" ? "active" : ""} onClick={() => setKicadTab("appearance")}>
                3D外观
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
              <span>{kicadTab === "top" ? "KiCad/KRT 顶层真实 SVG" : kicadTab === "bottom" ? "KiCad/KRT 底层真实 SVG" : "KiCad 3D真实渲染"}</span>
              <span>KRT autorouted .kicad_pcb</span>
              <span>{kicadRender.unconnectedPads} unconnected pads</span>
              <span>{kicadRender.drcViolations} DRC violations</span>
            </div>
            <p>{kicadRender.pcb}</p>
          </div>
          ) : (
          <div className="interactive-preview">
            <div className="layer-tabs" aria-label="草图视图">
              <button className={sketchView === "top" ? "active" : ""} onClick={() => setSketchView("top")}>
                顶层预布线
              </button>
              <button className={sketchView === "bottom" ? "active" : ""} onClick={() => setSketchView("bottom")}>
                底层预布线
              </button>
              <button className={sketchView === "three" ? "active" : ""} onClick={() => setSketchView("three")}>
                3D拖拽
              </button>
            </div>
            {sketchView === "three" ? (
              <Board3D
                design={design}
                onPlacementChange={handlePlacementChange}
                onSelectModule={setSelectedModuleId}
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
            <p className="interaction-hint">
              草图用于实时布局、方向调整、固定孔和层规划；虚线会跟随模块位置更新，铜线以 KiCad/KRT 输出为准。
            </p>
          </div>
          )}
          <GenerateMotionOverlay design={design} phase={assemblyPhase} />
        </div>
      </section>
      <section className="panel">
        <h2>设计与采购</h2>
        <div className="control-strip">
          <button className="primary" onClick={() => void regeneratePcbRouting()} disabled={routeRun.status === "running"}>
            {routeRun.status === "running" ? "正在生成..." : "重新生成PCB布线"}
          </button>
          <span className={`route-status ${routeRun.status}`}>{routeRun.message}</span>
        </div>
        <div className="edit-controls">
          <div className="selected-module">
            <span>选中模块</span>
            <strong>{selectedModule?.name ?? "点击草图中的模块"}</strong>
            {selectedPlacement ? <small>{selectedPlacement.rotation}° / {lockedModuleIds.includes(selectedPlacement.moduleId) ? "已锁定" : "可移动"}</small> : null}
          </div>
          <button onClick={rotateSelected} disabled={!selectedModule || lockedModuleIds.includes(selectedModule.id)}>
            旋转90°
          </button>
          <button onClick={toggleSelectedLock} disabled={!selectedModule}>
            {selectedModule && lockedModuleIds.includes(selectedModule.id) ? "解锁模块" : "锁定模块"}
          </button>
          <label className="toggle-control">
            <input type="checkbox" checked={snapToGrid} onChange={(event) => setSnapToGrid(event.target.checked)} />
            2.54mm吸附
          </label>
          <button className={holePlacementEnabled ? "active-tool" : ""} onClick={() => setHolePlacementEnabled((current) => !current)}>
            {holePlacementEnabled ? "固定孔放置中" : "放置固定孔"}
          </button>
          <label className="number-control">
            孔径
            <input
              aria-label="固定孔孔径"
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
            四角孔
          </button>
          <button onClick={() => setMountingHoles((current) => current.slice(0, -1))} disabled={mountingHoles.length === 0}>
            撤销孔
          </button>
        </div>
        <h2>模块素材</h2>
        <div className="source-summary">
          <span>{design.selectedModules.length} 个模块卡片</span>
          <span>Amazon 图 + 采购链接</span>
          <span>LCSC 备选采购</span>
        </div>
        <div className="source-grid">
          {design.selectedModules.map(({ module }) => (
            <SourceCard key={module.id} module={module} />
          ))}
        </div>
        <h2>固定孔 / 机械检查</h2>
        <div className={`hole-summary ${holePlacementEnabled ? "active" : ""}`}>
          <strong>固定孔 {mountingHoles.length} 个</strong>
          <span>{holePlacementEnabled ? "在 2D 或 3D 草图板面点击即可放置" : "打开放置模式后可继续加孔"}</span>
          <div className="hole-coordinate-list">
            {mountingHoles.map((hole) => (
              <small key={hole.id}>
                {hole.x.toFixed(1)}, {hole.y.toFixed(1)} / {hole.diameterMm.toFixed(1)}mm
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
        <h2>布局理由</h2>
        <div className="reason-list">
          {design.placements.map((placement) => (
            <p key={placement.moduleId}>
              <strong>{moduleById(placement.moduleId).name}</strong>：{placement.role}
            </p>
          ))}
        </div>
        <h2>2D布局草图</h2>
        <div className="mini-board">
          <BoardSvg
            design={design}
            onPlacementChange={handlePlacementChange}
            onSelectModule={setSelectedModuleId}
            mountingHoles={mountingHoles}
            selectedModuleId={selectedModuleId}
            lockedModuleIds={lockedModuleIds}
            mode="layout"
          />
        </div>
      </section>
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

function ExportView({ design }: { design: BoardDesign }) {
  const easyEdaJson = useMemo(() => JSON.stringify(buildEasyEdaProjectJson(design), null, 2), [design]);
  const bomCsv = useMemo(() => buildBomCsv(design), [design]);
  const reportJson = useMemo(() => buildReportJson(design), [design]);

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
    <div className="grid two">
      <section className="panel">
        <h2>3D底板预览</h2>
        <Board3D design={design} />
      </section>
      <section className="panel">
        <h2>BOM / 检查 / 导出</h2>
        <div className="summary-strip">
          <span>{design.bom.length} 模块</span>
          <span>${design.bom.reduce((sum, line) => sum + line.unitPriceUsd * line.qty, 0).toFixed(2)} 估算</span>
          <span>{design.report.summary.warn} 警告</span>
        </div>
        <div className="export-actions">
          <button onClick={() => void downloadExportZip(design)}>下载JLC工程ZIP</button>
          <button onClick={() => downloadText("board.easyeda.json", easyEdaJson, "application/json")}>EasyEDA JSON</button>
          <button onClick={() => downloadText("bom.csv", bomCsv, "text/csv")}>BOM CSV</button>
          <button onClick={() => downloadText("report.json", reportJson, "application/json")}>检查报告</button>
        </div>
        <div className="checks">
          <CheckList title="ERC" items={design.report.erc} />
          <CheckList title="DRC" items={design.report.drc} />
          <CheckList title="DFM" items={design.report.dfm} />
        </div>
      </section>
    </div>
  );
}

function PromptView({
  prompt,
  onPrompt,
  onGenerate,
}: {
  prompt: string;
  onPrompt: (value: string) => void;
  onGenerate: () => void;
}) {
  return (
    <div className="grid two">
      <section className="panel">
        <h2>自然语言需求</h2>
        <textarea value={prompt} onChange={(event) => onPrompt(event.target.value)} />
        <div className="actions">
          <button className="primary" onClick={onGenerate}>
            生成模块底板
          </button>
        </div>
      </section>
      <section className="panel">
        <h2>常用需求模板</h2>
        <div className="case-list">
          {demoCases.map((item) => (
            <button
              key={item.id}
              onClick={() => {
                onPrompt(item.prompt);
              }}
            >
              <strong>{item.name}</strong>
              <span>{item.prompt}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

export default function App() {
  const [prompt, setPrompt] = useState(defaultPrompt);
  const [active, setActive] = useState<Step>("Generate");
  const [design, setDesign] = useState(() => generateDesign(defaultPrompt));
  const [generationKey, setGenerationKey] = useState(0);
  const bomTotal = useMemo(
    () => design.bom.reduce((sum, line) => sum + line.unitPriceUsd * line.qty, 0),
    [design.bom],
  );

  const runGenerate = () => {
    setDesign(generateDesign(prompt));
    setGenerationKey((current) => current + 1);
    setActive("Generate");
  };

  const updatePlacement = (moduleId: string, x: number, y: number) => {
    setDesign((current) => {
      const target = moduleById(moduleId);
      const placements = current.placements.map((placement) =>
        placement.moduleId === moduleId
          ? clampPlacement(target, { ...placement, x, y }, current.boardSizeMm)
          : placement,
      );

      const tracks = routeNets(current.nets, placements, current.boardSizeMm, current.keepouts);
      const nextDesign = {
        ...current,
        placements,
        tracks,
      };
      return { ...nextDesign, report: checkDesign(nextDesign) };
    });
  };

  const rotateModule = (moduleId: string) => {
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
      return { ...nextDesign, report: checkDesign(nextDesign) };
    });
  };

  const rerouteSketch = () => {
    setDesign((current) => {
      const nextDesign = {
        ...current,
        tracks: routeNets(current.nets, current.placements, current.boardSizeMm, current.keepouts),
      };
      return { ...nextDesign, report: checkDesign(nextDesign) };
    });
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Blockless PCB Studio</p>
          <h1>模块化PCB设计工作台</h1>
        </div>
        <div className="header-stats">
          <span>模块选型</span>
          <span>JLC EDA导出</span>
          <span>自动布局布线</span>
          <span>STEP/STL 3D预览</span>
        </div>
      </header>

      <div className="workspace-layout">
        <aside className="workspace-rail">
          <section className="rail-card">
            <p className="eyebrow">Project</p>
            <strong>{design.title}</strong>
            <div className="summary-strip">
              <span>
                {design.boardSizeMm.w} x {design.boardSizeMm.h} mm
              </span>
              <span>{design.selectedModules.length} 模块</span>
            </div>
          </section>
          <section className="rail-card">
            <p className="eyebrow">Workspace</p>
            <Stepper active={active} onChange={setActive} />
          </section>
          <section className="rail-card">
            <p className="eyebrow">Status</p>
            <div className="summary-strip">
              <span>{design.report.summary.pass} pass</span>
              <span>{design.report.summary.warn} warn</span>
              <span>{design.report.summary.fail} fail</span>
              <span>${bomTotal.toFixed(2)}</span>
            </div>
          </section>
        </aside>

        <section className="workspace-stage">
          <div className="stage-head">
            <div>
              <p className="eyebrow">Active View</p>
              <h2>{stepLabels[active]}</h2>
            </div>
            <div className="stage-metrics">
              <span>{design.selectedModules.length} modules</span>
              <span>{design.nets.length} nets</span>
              <span>
                {design.boardSizeMm.w} x {design.boardSizeMm.h} mm
              </span>
              <span>{design.report.summary.warn} warn</span>
            </div>
          </div>

          <div className="workspace-view">
            {active === "Prompt" && <PromptView prompt={prompt} onPrompt={setPrompt} onGenerate={runGenerate} />}
            {active === "Plan" && <PlanView design={design} />}
            {active === "Review" && <ReviewView design={design} />}
            {active === "Generate" && (
              <GenerateView
                design={design}
                generationKey={generationKey}
                onPlacementChange={updatePlacement}
                onRotateModule={rotateModule}
                onRerouteSketch={rerouteSketch}
              />
            )}
            {active === "Export" && <ExportView design={design} />}
          </div>
        </section>
      </div>

      <footer>
        <span>工作流：商用模块库 + 接口匹配 + KiCad/KRT布线校验</span>
        <span>输出：JLC EDA工程包、BOM、检查报告和3D预览</span>
      </footer>
    </main>
  );
}
