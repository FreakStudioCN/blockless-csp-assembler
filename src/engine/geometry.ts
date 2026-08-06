import type { HardwareModule, Placement } from "../domain/types";

export function rotatedSize(module: HardwareModule, placement: Placement) {
  const isRotated = placement.rotation === 90 || placement.rotation === 270;
  return {
    w: isRotated ? module.sizeMm.h : module.sizeMm.w,
    h: isRotated ? module.sizeMm.w : module.sizeMm.h,
  };
}

export function clampPlacement(module: HardwareModule, placement: Placement, boardSize: { w: number; h: number }): Placement {
  const size = rotatedSize(module, placement);
  return {
    ...placement,
    x: Math.min(boardSize.w - size.w / 2 - 1.5, Math.max(size.w / 2 + 1.5, placement.x)),
    y: Math.min(boardSize.h - size.h / 2 - 1.5, Math.max(size.h / 2 + 1.5, placement.y)),
  };
}

function localPinPoint(module: HardwareModule, pinName: string) {
  const index = Math.max(0, module.pins.findIndex((pin) => pin.name === pinName));
  const pin = module.pins[index] ?? module.pins[0];
  const pitch = 2.54;
  const lane = Math.min(module.sizeMm.h / 2 - 2.5, 4 + (index % 12) * pitch);

  if (!pin) return { x: 0, y: 0 };
  if (pin.type === "power_in" || pin.type === "power_out") return { x: -module.sizeMm.w / 2, y: -module.sizeMm.h / 2 + lane };
  if (pin.type === "gnd") return { x: -module.sizeMm.w / 2, y: module.sizeMm.h / 2 - lane };
  if (pin.type.startsWith("i2c")) return { x: module.sizeMm.w / 2, y: -module.sizeMm.h / 2 + lane };
  if (pin.type.startsWith("uart")) return { x: module.sizeMm.w / 2, y: module.sizeMm.h / 2 - lane };
  return { x: 0, y: module.sizeMm.h / 2 };
}

export function getPinPoint(module: HardwareModule, placement: Placement, pinName: string) {
  const local = localPinPoint(module, pinName);
  const angle = (placement.rotation * Math.PI) / 180;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);

  return {
    x: placement.x + local.x * cos - local.y * sin,
    y: placement.y + local.x * sin + local.y * cos,
  };
}

export function pcbToScene(point: { x: number; y: number }, boardSize: { w: number; h: number }, scale: number) {
  return {
    x: (point.x - boardSize.w / 2) * scale,
    z: (point.y - boardSize.h / 2) * scale,
  };
}
