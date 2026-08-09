import { planModules } from "./planner";
import { matchInterfaces } from "./matcher";
import { placeModules } from "./placer";
import { routeNets } from "./router";
import { buildBom, checkDesign } from "./checker";
import type { BoardDesign } from "../domain/types";

export function generateDesign(prompt: string, locale: "zh" | "en" = "zh"): BoardDesign {
  const { demoCase, selectedModules } = planModules(prompt, locale);
  const nets = matchInterfaces(selectedModules);
  const { placements, keepouts } = placeModules(demoCase, selectedModules);
  const tracks = routeNets(nets, placements, demoCase.boardSizeMm, keepouts);
  const title = locale === "en" ? demoCase.nameEn ?? demoCase.name : demoCase.name;

  const partial = {
    id: demoCase.id,
    title,
    prompt,
    boardSizeMm: demoCase.boardSizeMm,
    selectedModules,
    nets,
    placements,
    tracks,
    keepouts,
  };

  const bom = buildBom(selectedModules);
  const report = checkDesign(partial, locale);

  return {
    ...partial,
    bom,
    report,
  };
}
