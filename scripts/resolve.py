import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATHS = [ROOT / "imports" / "cccs-mods-profile" / "cccs-mods-profile.json"]
PROFILE_PATHS += sorted((ROOT / "profiles").glob("*/*-profile.json"))
SPEC = importlib.util.spec_from_file_location("catalog_to_csv", Path(__file__).with_name("catalog-to-csv.py"))
if SPEC is None or SPEC.loader is None:
    raise ImportError("Cannot load catalog-to-csv.py")
CSV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CSV)


def portable_links(value, destination: Path) -> None:
    if isinstance(value, dict):
        href = value.get("href")
        if isinstance(href, str) and href.startswith("file:"):
            uri = urllib.parse.urlsplit(href)
            filename = urllib.request.url2pathname(uri.path)
            local = Path(filename)
            if local.is_relative_to(ROOT):
                relative = Path(os.path.relpath(local, destination.parent)).as_posix()
                value["href"] = urllib.parse.quote(relative, safe="/+.-") + (
                    "#" + uri.fragment if uri.fragment else ""
                )
        for child in value.values():
            portable_links(child, destination)
    elif isinstance(value, list):
        for child in value:
            portable_links(child, destination)


def normalize(catalog: dict, profile: dict, destination: Path) -> None:
    portable_links(catalog, destination)
    tool_props = catalog["metadata"].get("props", [])
    catalog["metadata"] = copy.deepcopy(profile["metadata"])
    if tool_props:
        catalog["metadata"].setdefault("props", []).extend(tool_props)
    catalog["metadata"].setdefault("links", []).append({
        "href": destination.name.replace("-resolved.json", "-profile.json"),
        "rel": "source-profile",
    })
    catalog.pop("uuid", None)
    content_hash = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode("utf-8")).hexdigest()
    catalog["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, profile["uuid"] + ":" + content_hash))


def serialize(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate, resolve and export all CCCS profiles.")
    parser.add_argument("--oscal-cli", default=os.environ.get("OSCAL_CLI", "oscal-cli"))
    parser.add_argument("--check", action="store_true", help="Fail if committed outputs differ from a fresh resolution.")
    args = parser.parse_args()
    cli = shutil.which(args.oscal_cli)
    if cli is None:
        parser.error("oscal-cli is missing. Install version 3.2.0+ and set OSCAL_CLI or --oscal-cli.")
    result = subprocess.run([cli, "--version"], check=True, capture_output=True, text=True, encoding="utf-8")
    version_text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout)
    match = re.search(r"oscal-cli (\d+)\.(\d+)\.(\d+)", version_text)
    if not match or tuple(map(int, match.groups())) < (3, 2, 0):
        parser.error("oscal-cli 3.2.0 or newer is required")
    print(version_text.splitlines()[0], flush=True)

    def run(*arguments) -> None:
        subprocess.run([cli, *map(str, arguments)], check=True, cwd=ROOT)

    catalogs = sorted((ROOT / "imports").glob("*/*-catalog.json"))
    for path in catalogs + PROFILE_PATHS:
        run("validate", path)
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="resolve-", dir=cache) as temporary:
        outputs = []
        for index, path in enumerate(PROFILE_PATHS):
            profile = json.loads(path.read_text(encoding="utf-8"))["profile"]
            destination = path.with_name(path.name.replace("-profile.json", "-resolved.json"))
            staged = Path(temporary) / f"{index}.json"
            print(f"Resolving {path.relative_to(ROOT)}", flush=True)
            run("resolve-profile", "--to=json", "--overwrite", path, staged)
            resolved = json.loads(staged.read_text(encoding="utf-8"))
            normalize(resolved["catalog"], profile, destination)
            staged.write_text(serialize(resolved), encoding="utf-8", newline="\n")
            run("validate", staged)
            csv_path = staged.with_suffix(".csv")
            CSV.convert(staged, csv_path)
            outputs.extend(((staged, destination), (csv_path, destination.with_suffix(".csv"))))
        if args.check:
            changed = [str(dest.relative_to(ROOT)) for source, dest in outputs if not dest.exists() or source.read_bytes() != dest.read_bytes()]
            if changed:
                raise ValueError("Resolved outputs are out of date:\n" + "\n".join(changed))
        else:
            for source, destination in outputs:
                os.replace(source, destination)
    print("All seven profiles validated, resolved and exported successfully.", flush=True)


if __name__ == "__main__":
    main()
