import { planModules } from "./planner";
import { matchInterfaces } from "./matcher";
import { placeModules } from "./placer";
import { routeNets } from "./router";
import { buildBom, checkDesign } from "./checker";
import type { BoardDesign } from "../domain/types";

export function generateDesign(prompt: string): BoardDesign {
  const { demoCase, selectedModules } = planModules(prompt);
  const nets = matchInterfaces(selectedModules);
  const { placements, keepouts } = placeModules(demoCase, selectedModules);
  const tracks = routeNets(nets, placements, demoCase.boardSizeMm, keepouts);

  const partial = {
    id: demoCase.id,
    title: demoCase.name,
    prompt,
    boardSizeMm: demoCase.boardSizeMm,
    selectedModules,
    nets,
    placements,
    tracks,
    keepouts,
  };

  const bom = buildBom(selectedModules);
  const report = checkDesign(partial);

  return {
    ...partial,
    bom,
    report,
  };
}
