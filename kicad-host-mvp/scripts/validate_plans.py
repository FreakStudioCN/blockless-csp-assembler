import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def endpoint_parts(endpoint):
    if "." not in endpoint:
        raise ValueError(f"Endpoint must be module.pin: {endpoint}")
    return endpoint.split(".", 1)


def validate_plan(path):
    plan = json.loads(path.read_text(encoding="utf-8"))
    errors = []
    modules = {module["id"]: module for module in plan.get("modules", [])}

    if not modules:
        errors.append("plan has no modules")

    for module in modules.values():
        pins = module.get("pins", [])
        if not pins:
            errors.append(f"{module['id']} has no pins")
        if len(module.get("size_mm", [])) != 2:
            errors.append(f"{module['id']} size_mm must be [w,h]")
        if len(module.get("placement", [])) != 3:
            errors.append(f"{module['id']} placement must be [x,y,rotation]")

    for net in plan.get("nets", []):
        if len(net) != 3:
            errors.append(f"bad net tuple: {net}")
            continue
        net_name, source, targets = net
        endpoints = [source] + targets
        for endpoint in endpoints:
            module_id, pin = endpoint_parts(endpoint)
            module = modules.get(module_id)
            if not module:
                errors.append(f"{path.name}:{net_name} references missing module {module_id}")
                continue
            if pin not in module.get("pins", []):
                errors.append(f"{path.name}:{net_name} references missing pin {endpoint}")

    if errors:
        raise SystemExit("\n".join(errors))
    print(f"valid {path.name}")


def main():
    for path in sorted((ROOT / "plans").glob("*.json")):
        validate_plan(path)


if __name__ == "__main__":
    main()
