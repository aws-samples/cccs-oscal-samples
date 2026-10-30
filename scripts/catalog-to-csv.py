import argparse
import csv
import json
import re
from pathlib import Path


INSERT = re.compile(r"\{\{\s*insert:\s*param,\s*([^\s}]+)\s*\}\}")
HEADERS = [
    "Row Sort ID", "Control ID", "Statement ID", "Control Statement", "Supplemental Guidance",
    "Control Title", "Source References", "Additional Information",
]


def walk(container: dict):
    yield container
    for key in ("groups", "controls"):
        for child in container.get(key, []):
            yield from walk(child)


def walk_controls(container: dict):
    for control in container.get("controls", []):
        yield control
        yield from walk_controls(control)
    for group in container.get("groups", []):
        yield from walk_controls(group)


def render_text(text: str, params: dict, ancestors: tuple = ()) -> str:
    def replace(match: re.Match) -> str:
        param_id = match[1]
        if param_id in ancestors:
            raise ValueError(f"Cyclic parameter reference: {' -> '.join(ancestors + (param_id,))}")
        if param_id not in params:
            raise ValueError(f"Unresolved parameter: {param_id}")
        param = params[param_id]
        nested = ancestors + (param_id,)
        if "values" in param:
            if not param["values"]:
                raise ValueError(f"Parameter has no assigned values: {param_id}")
            value = "; ".join(render_text(value, params, nested) for value in param["values"])
            return f"[CCCS Assignment: {value}]"
        if "select" in param:
            choices = param["select"].get("choice", [])
            if not choices:
                raise ValueError(f"Selection has no choices: {param_id}")
            cardinality = " (one or more)" if param["select"].get("how-many") == "one-or-more" else ""
            value = "; ".join(render_text(choice, params, nested) for choice in choices)
            return f"[Selection{cardinality}: {value}]"
        label = next(
            (p["value"] for p in param.get("props", []) if p["name"] == "alt-label"),
            param.get("label"),
        )
        if not label:
            raise ValueError(f"Parameter has no assignment label: {param_id}")
        label = re.sub(r"^organization-defined\s+", "", label, flags=re.IGNORECASE)
        return f"[Assignment: organization-defined {render_text(label, params, nested)}]"

    return INSERT.sub(replace, text)


def part_text(part: dict, params: dict) -> str:
    texts = [render_text(part["prose"], params)] if part.get("prose") else []
    texts.extend(part_text(child, params) for child in part.get("parts", []))
    return "\n\n".join(text for text in texts if text)


def natural_key(text: str) -> list:
    return [int(token) if token.isdigit() else token for token in re.split(r"(\d+)", text)]


def catalog_to_rows(catalog: dict) -> list[list[str]]:
    params = {}
    for container in walk(catalog):
        for param in container.get("params", []):
            if param["id"] in params:
                raise ValueError(f"Duplicate parameter ID: {param['id']}")
            params[param["id"]] = param
    rows = []
    for control in walk_controls(catalog):
        props = {p["name"]: p["value"] for p in control.get("props", [])}
        if props.get("status") == "withdrawn":
            continue
        statements = [part for part in control["parts"] if part["name"] == "statement"]
        if not statements:
            raise ValueError(f"Active control has no statement: {control['id']}")
        if "sort-id" not in props:
            raise ValueError(f"Control has no sort-id: {control['id']}")
        guidance = "\n\n".join(
            part_text(part, params) for part in control["parts"] if part["name"] == "guidance"
        )
        additional = "\n\n".join(
            (f"**{part['title']}**\n\n" if part.get("title") else "") + part_text(part, params)
            for part in control["parts"] if part["name"] not in ("statement", "guidance")
        )
        references = "\n".join(
            link["href"] for link in control.get("links", []) if link.get("rel") == "reference"
        )
        first_row = True

        def append_statement(statement: dict) -> None:
            nonlocal first_row
            statement_id = statement.get("id", "").split("_smt.", 1)[-1].upper() if "_smt." in statement.get("id", "") else ""
            rows.append([
                f"{props['sort-id']}-{statement_id}", control["id"].upper(), statement_id,
                render_text(statement.get("prose", ""), params),
                guidance if first_row else "", control["title"], references if first_row else "",
                additional if first_row else "",
            ])
            first_row = False
            for child in statement.get("parts", []):
                append_statement(child)

        for statement in statements:
            append_statement(statement)
    rows.sort(key=lambda row: natural_key(row[0]))
    return rows


def convert(catalog_path: Path, output_path: Path) -> None:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))["catalog"]
    rows = catalog_to_rows(catalog)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(HEADERS)
        writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export OSCAL control statements and guidance to UTF-8 CSV.")
    parser.add_argument("catalog", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    convert(args.catalog, args.output)
