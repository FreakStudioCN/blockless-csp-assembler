import type { Keepout, NetConnection, Placement, TrackSegment } from "../domain/types";
import { getModule } from "../data/modules";
import { getPinPoint, rotatedSize } from "./geometry";

type Point = { x: number; y: number };
type Rect = { x1: number; y1: number; x2: number; y2: number };
type RouteQuality = "interactive" | "polished";

const GRID_MM = 3;
const EDGE_MARGIN_MM = 2;
const MODULE_CLEARANCE_MM = 1.8;
const TRACK_CLEARANCE_MM = 1.2;

function key(point: Point) {
  return `${point.x.toFixed(1)},${point.y.toFixed(1)}`;
}

function distance(a: Point, b: Point) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function snap(point: Point, boardSize: { w: number; h: number }) {
  return {
    x: clamp(Math.round(point.x / GRID_MM) * GRID_MM, EDGE_MARGIN_MM, boardSize.w - EDGE_MARGIN_MM),
    y: clamp(Math.round(point.y / GRID_MM) * GRID_MM, EDGE_MARGIN_MM, boardSize.h - EDGE_MARGIN_MM),
  };
}

function moduleRect(placement: Placement, clearance = MODULE_CLEARANCE_MM): Rect {
  const size = rotatedSize(getModule(placement.moduleId), placement);
  return {
    x1: placement.x - size.w / 2 - clearance,
    y1: placement.y - size.h / 2 - clearance,
    x2: placement.x + size.w / 2 + clearance,
    y2: placement.y + size.h / 2 + clearance,
  };
}

function keepoutRect(keepout: Keepout): Rect {
  return {
    x1: keepout.x - MODULE_CLEARANCE_MM,
    y1: keepout.y - MODULE_CLEARANCE_MM,
    x2: keepout.x + keepout.w + MODULE_CLEARANCE_MM,
    y2: keepout.y + keepout.h + MODULE_CLEARANCE_MM,
  };
}

function contains(rect: Rect, point: Point) {
  return point.x >= rect.x1 && point.x <= rect.x2 && point.y >= rect.y1 && point.y <= rect.y2;
}

function rectDistance(rect: Rect, point: Point) {
  const dx = Math.max(rect.x1 - point.x, 0, point.x - rect.x2);
  const dy = Math.max(rect.y1 - point.y, 0, point.y - rect.y2);
  return Math.hypot(dx, dy);
}

function segmentDistance(point: Point, a: Point, b: Point) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lengthSq = dx * dx + dy * dy;
  if (lengthSq === 0) return distance(point, a);
  const t = clamp(((point.x - a.x) * dx + (point.y - a.y) * dy) / lengthSq, 0, 1);
  return distance(point, { x: a.x + t * dx, y: a.y + t * dy });
}

function almostSamePoint(a: Point, b: Point) {
  return distance(a, b) < 0.2;
}

function orientation(a: Point, b: Point, c: Point) {
  const value = (b.y - a.y) * (c.x - b.x) - (b.x - a.x) * (c.y - b.y);
  if (Math.abs(value) < 0.0001) return 0;
  return value > 0 ? 1 : 2;
}

function segmentsIntersect(a: Point, b: Point, c: Point, d: Point) {
  if (almostSamePoint(a, c) || almostSamePoint(a, d) || almostSamePoint(b, c) || almostSamePoint(b, d)) return false;
  const o1 = orientation(a, b, c);
  const o2 = orientation(a, b, d);
  const o3 = orientation(c, d, a);
  const o4 = orientation(c, d, b);
  return o1 !== o2 && o3 !== o4;
}

function pointBlocked(point: Point, obstacles: Rect[], start: Point, end: Point) {
  if (distance(point, start) < GRID_MM * 1.4 || distance(point, end) < GRID_MM * 1.4) return false;
  return obstacles.some((rect) => contains(rect, point));
}

function segmentBlocked(a: Point, b: Point, obstacles: Rect[], start: Point, end: Point) {
  const steps = Math.max(2, Math.ceil(distance(a, b) / 0.8));
  for (let index = 1; index <= steps; index += 1) {
    const point = {
      x: a.x + ((b.x - a.x) * index) / steps,
      y: a.y + ((b.y - a.y) * index) / steps,
    };
    if (pointBlocked(point, obstacles, start, end)) return true;
  }
  return false;
}

function clearancePenalty(point: Point, obstacles: Rect[]) {
  return obstacles.reduce((penalty, rect) => {
    const gap = rectDistance(rect, point);
    if (gap < 0.8) return penalty + 25;
    if (gap < 2.4) return penalty + (2.4 - gap) * 3;
    return penalty;
  }, 0);
}

function reservedTrackPenalty(point: Point, routed: TrackSegment[], currentNet: string, layer: TrackSegment["layer"]) {
  let penalty = 0;
  for (const track of routed) {
    if (track.net === currentNet) continue;
    for (let index = 0; index < track.points.length - 1; index += 1) {
      const gap = segmentDistance(point, track.points[index], track.points[index + 1]);
      if (gap < TRACK_CLEARANCE_MM && track.layer === layer) penalty += 40;
      else if (gap < TRACK_CLEARANCE_MM) penalty += 8;
    }
  }
  return penalty;
}

function reservedSegmentPenalty(a: Point, b: Point, routed: TrackSegment[], currentNet: string, layer: TrackSegment["layer"]) {
  let penalty = 0;
  for (const track of routed) {
    if (track.net === currentNet) continue;
    for (let index = 0; index < track.points.length - 1; index += 1) {
      const c = track.points[index];
      const d = track.points[index + 1];
      if (segmentsIntersect(a, b, c, d)) penalty += track.layer === layer ? 160 : 18;
      const midpoint = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const gap = segmentDistance(midpoint, c, d);
      if (gap < TRACK_CLEARANCE_MM * 0.75 && track.layer === layer) penalty += 34;
    }
  }
  return penalty;
}

function simplify(points: Point[]) {
  const deduped = points.filter((point, index) => index === 0 || distance(point, points[index - 1]) > 0.05);
  const simplified: Point[] = [];
  for (const point of deduped) {
    simplified.push(point);
    while (simplified.length >= 3) {
      const a = simplified[simplified.length - 3];
      const b = simplified[simplified.length - 2];
      const c = simplified[simplified.length - 1];
      const cross = (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x);
      if (Math.abs(cross) > 0.01) break;
      simplified.splice(simplified.length - 2, 1);
    }
  }
  return simplified;
}

function softenCorners(points: Point[]) {
  if (points.length < 3) return points;
  const softened: Point[] = [points[0]];
  for (let index = 1; index < points.length - 1; index += 1) {
    const previous = points[index - 1];
    const current = points[index];
    const next = points[index + 1];
    const inLength = distance(previous, current);
    const outLength = distance(current, next);
    const cross = (current.x - previous.x) * (next.y - current.y) - (current.y - previous.y) * (next.x - current.x);
    if (Math.abs(cross) < 0.01 || inLength < 1.8 || outLength < 1.8) {
      softened.push(current);
      continue;
    }

    const cut = Math.min(1.35, inLength * 0.32, outLength * 0.32);
    softened.push({
      x: current.x + ((previous.x - current.x) / inLength) * cut,
      y: current.y + ((previous.y - current.y) / inLength) * cut,
    });
    softened.push({
      x: current.x + ((next.x - current.x) / outLength) * cut,
      y: current.y + ((next.y - current.y) / outLength) * cut,
    });
  }
  softened.push(points[points.length - 1]);
  return softened;
}

function reconstruct(cameFrom: Map<string, string>, pointsByKey: Map<string, Point>, endKey: string) {
  const points: Point[] = [];
  let cursor: string | undefined = endKey;
  while (cursor) {
    const point = pointsByKey.get(cursor);
    if (point) points.push(point);
    cursor = cameFrom.get(cursor);
  }
  return points.reverse();
}

function fallbackRoute(start: Point, end: Point) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const diagonal = Math.min(Math.abs(dx), Math.abs(dy)) * 0.55;
  if (diagonal < 1.5) return [start, end];

  return [
    start,
    { x: start.x + Math.sign(dx) * diagonal, y: start.y + Math.sign(dy) * diagonal },
    { x: end.x - Math.sign(dx) * diagonal, y: end.y - Math.sign(dy) * diagonal },
    end,
  ];
}

function polishRoute(points: Point[]) {
  return softenCorners(simplify(points));
}

function routePriority(net: NetConnection) {
  const kindPriority: Record<NetConnection["kind"], number> = {
    power: 0,
    gnd: 1,
    spi: 2,
    i2c: 3,
    uart: 4,
    gpio: 5,
  };
  return kindPriority[net.kind] * 100 - net.widthMm * 10 + (net.layer === "bottom" ? 8 : 0);
}

function routeAStar({
  start,
  end,
  boardSize,
  obstacles,
  routed,
  net,
  layer,
  quality,
}: {
  start: Point;
  end: Point;
  boardSize: { w: number; h: number };
  obstacles: Rect[];
  routed: TrackSegment[];
  net: string;
  layer: TrackSegment["layer"];
  quality: RouteQuality;
}) {
  const startNode = snap(start, boardSize);
  const endNode = snap(end, boardSize);
  const startKey = key(startNode);
  const endKey = key(endNode);
  const open = new Set([startKey]);
  const cameFrom = new Map<string, string>();
  const pointsByKey = new Map<string, Point>([
    [startKey, startNode],
    [endKey, endNode],
  ]);
  const gScore = new Map<string, number>([[startKey, 0]]);
  const fScore = new Map<string, number>([[startKey, distance(startNode, endNode)]]);
  const moves = [
    { x: GRID_MM, y: 0 },
    { x: -GRID_MM, y: 0 },
    { x: 0, y: GRID_MM },
    { x: 0, y: -GRID_MM },
    { x: GRID_MM, y: GRID_MM },
    { x: GRID_MM, y: -GRID_MM },
    { x: -GRID_MM, y: GRID_MM },
    { x: -GRID_MM, y: -GRID_MM },
  ];

  let iterations = 0;
  while (open.size > 0 && iterations < 1800) {
    iterations += 1;
    let currentKey = "";
    let currentScore = Number.POSITIVE_INFINITY;
    for (const candidate of open) {
      const score = fScore.get(candidate) ?? Number.POSITIVE_INFINITY;
      if (score < currentScore) {
        currentScore = score;
        currentKey = candidate;
      }
    }

    const current = pointsByKey.get(currentKey);
    if (!current) break;
    if (currentKey === endKey) {
      const route = [start, ...reconstruct(cameFrom, pointsByKey, currentKey).slice(1, -1), end];
      return quality === "polished" ? polishRoute(route) : simplify(route);
    }

    open.delete(currentKey);
    for (const move of moves) {
      const next = {
        x: clamp(current.x + move.x, EDGE_MARGIN_MM, boardSize.w - EDGE_MARGIN_MM),
        y: clamp(current.y + move.y, EDGE_MARGIN_MM, boardSize.h - EDGE_MARGIN_MM),
      };
      const nextKey = key(next);
      if (nextKey === currentKey) continue;
      if (pointBlocked(next, obstacles, start, end)) continue;
      if (segmentBlocked(current, next, obstacles, start, end)) continue;

      const baseCost = distance(current, next);
      const diagonalBias = Math.abs(move.x) > 0 && Math.abs(move.y) > 0 ? -0.15 : 0;
      const tentative =
        (gScore.get(currentKey) ?? Number.POSITIVE_INFINITY) +
        baseCost +
        diagonalBias +
        clearancePenalty(next, obstacles) +
        reservedTrackPenalty(next, routed, net, layer) +
        (quality === "polished" ? reservedSegmentPenalty(current, next, routed, net, layer) : 0);

      if (tentative >= (gScore.get(nextKey) ?? Number.POSITIVE_INFINITY)) continue;
      cameFrom.set(nextKey, currentKey);
      pointsByKey.set(nextKey, next);
      gScore.set(nextKey, tentative);
      fScore.set(nextKey, tentative + distance(next, endNode));
      open.add(nextKey);
    }
  }

  const route = fallbackRoute(start, end);
  return quality === "polished" ? polishRoute(route) : simplify(route);
}

export function routeNets(
  nets: NetConnection[],
  placements: Placement[],
  boardSize: { w: number; h: number },
  keepouts: Keepout[] = [],
  options: { quality?: RouteQuality } = {},
): TrackSegment[] {
  const byModule = Object.fromEntries(placements.map((placement) => [placement.moduleId, placement]));
  const routed: TrackSegment[] = [];
  const quality = options.quality ?? "interactive";

  [...nets].sort((a, b) => routePriority(a) - routePriority(b)).forEach((net) => {
    const fromPlacement = byModule[net.from.moduleId];
    if (!fromPlacement) return;
    const from = getPinPoint(getModule(net.from.moduleId), fromPlacement, net.from.pin);

    net.to.forEach((target) => {
      const toPlacement = byModule[target.moduleId];
      if (!toPlacement) return;
      const to = getPinPoint(getModule(target.moduleId), toPlacement, target.pin);
      const ignored = new Set([net.from.moduleId, target.moduleId]);
      const obstacles = [
        ...placements
          .filter((placement) => !ignored.has(placement.moduleId))
          .map((placement) => moduleRect(placement)),
        ...keepouts.map(keepoutRect),
      ];
      const points = routeAStar({
        start: from,
        end: to,
        boardSize,
        obstacles,
        routed,
        net: net.net,
        layer: net.layer,
        quality,
      });

      routed.push({
        net: net.net,
        kind: net.kind,
        layer: net.layer,
        widthMm: net.widthMm,
        points,
      });
    });
  });

  return routed;
}
