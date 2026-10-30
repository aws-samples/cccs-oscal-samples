import argparse
import copy
import hashlib
import json
import re
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "imports" / "cccs-control-catalog" / "cccs-control-catalog.json"
LOCK = CATALOG.with_name("sources.lock.json")
REQUIREMENTS_CATALOG = ROOT / "imports" / "cccs-itsp.10.171-catalog" / "cccs-itsp.10.171-catalog.json"
BASE = "https://www.cyber.gc.ca"
GUIDANCE = "/en/guidance/cyber-security-privacy-risk-management/itsp10033"
MEDIUM = (
    "/en/guidance/cyber-security-privacy-risk-management/"
    "suggested-organizational-security-privacy-control-activity-profile-medium-impact-itsp10033-01"
)
ITSP171 = (
    "/en/guidance/"
    "protecting-specified-information-non-government-canada-systems-and-organizations-itsp10171"
)
NIST_COMMIT = "f0de6d8d8c059d374f15e0ae07508d82e370fc1d"
NIST_URL = (
    f"https://raw.githubusercontent.com/usnistgov/oscal-content/{NIST_COMMIT}/"
    "nist.gov/SP800-53/rev5/json/NIST_SP-800-53_rev5_catalog.json"
)
FAMILIES = {
    "ac": "access-control",
    "at": "awareness-training",
    "au": "audit-accountability",
    "ca": "assessment-authorization-monitoring",
    "cm": "configuration-management",
    "cp": "contingency-planning",
    "ia": "identification-authentication",
    "ir": "incident-response",
    "ma": "maintenance",
    "mp": "media-protection",
    "pe": "physical-environmental-protection",
    "pl": "planning",
    "pm": "program-management",
    "ps": "personnel-security",
    "pt": "personal-information-handling-transparency",
    "ra": "risk-assessment",
    "sa": "system-services-acquisition",
    "sc": "system-communications-protection",
    "si": "system-information-integrity",
    "sr": "supply-chain-risk-management",
}
NS = "https://github.com/kylecancode/cccs-oscal-samples/ns"
MODIFIED = "2026-10-30T18:29:00Z"
VERSION = "2.0.0"
OSCAL_VERSION = "1.2.3"
COPYRIGHT = (
    "Control text: Canadian Centre for Cyber Security, Security and privacy controls and "
    "assurance activities catalogue (ITSP.10.033), March 31, 2026; an unofficial reproduction "
    "of the publication at the reference URL. Source text is Crown copyright, reproduced "
    "under the Government of Canada non-commercial reproduction terms, not the repository's "
    "MIT-0 software license. Commercial redistribution requires permission. "
    "This transcription is not endorsed by the Government of Canada."
)
INSERT = re.compile(r"\{\{\s*insert:\s*param,\s*([^\s}]+)\s*\}\}")
CONTROL_LABEL = re.compile(r"\b([A-Z]{2})-(\d+)(?:\((\d+)\))?")


def control_id(label: str) -> str:
    match = CONTROL_LABEL.fullmatch(label.strip())
    if not match:
        raise ValueError(f"Invalid control label: {label!r}")
    family, number, enhancement = match.groups()
    result = f"{family.lower()}-{int(number)}"
    return result if enhancement is None else f"{result}.{int(enhancement)}"


def walk_controls(container: dict):
    for control in container.get("controls", []):
        yield control
        yield from walk_controls(control)
    for group in container.get("groups", []):
        yield from walk_controls(group)


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (CCCS-OSCAL)"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def source_page(path: str, cache: Path, refresh: bool) -> tuple[dict, str]:
    cached = cache / f"{path.rsplit('/', 1)[-1]}.json"
    if refresh or not cached.exists():
        url = f"{BASE}/api/cccs/page/v1/get?lang=en&url={path}"
        payload = download(url)
        json.loads(payload)["response"]["page"]
        cached.write_bytes(payload)
    page = json.loads(cached.read_text(encoding="utf-8"))["response"]["page"]
    body = "\n".join(page["body"])
    return {
        "url": BASE + path,
        "title": page["title"],
        "date-modified": page["date_modified"],
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }, body


def markdown(node, source_url: str = BASE) -> str:
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""
    if node.name in ("script", "style"):
        return ""
    if node.name in ("ul", "ol"):
        lines = []
        for index, item in enumerate(node.find_all("li", recursive=False), 1):
            text = "".join(markdown(child, source_url) for child in item.children).strip()
            marker = f"{index}. " if node.name == "ol" else "- "
            lines.append(marker + text.replace("\n", "\n" + " " * len(marker)))
        return "\n\n" + "\n".join(lines) + "\n\n"
    text = "".join(markdown(child, source_url) for child in node.children)
    if node.name == "a":
        href = node.get("href")
        return f"[{text.strip()}]({urllib.parse.urljoin(source_url, href)})" if href else text
    if node.name in ("strong", "b"):
        return f"**{text.strip()}**" if text.strip() else ""
    if node.name in ("em", "i"):
        return f"*{text.strip()}*" if text.strip() else ""
    if node.name == "br":
        return "\n"
    if node.name in ("p", "div", "blockquote", "h4", "h5"):
        return "\n\n" + text.strip() + "\n\n"
    return text


def prose(nodes: list, source_url: str = BASE) -> str:
    text = "".join(markdown(node, source_url) for node in nodes)
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip()


def is_blank(node) -> bool:
    if isinstance(node, Comment):
        return True
    if isinstance(node, NavigableString):
        return not str(node).strip()
    return isinstance(node, Tag) and node.name == "br"


def list_label(node: Tag, number: int) -> str:
    # Published list styles; CCCS-added items use start="27"/"54", rendered as AA/BB.
    classes = node.get("class", [])
    if "lst-upr-alph" in classes or "lst-lwr-alph" in classes:
        label = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            label = chr(ord("A") + remainder) + label
        return label if "lst-upr-alph" in classes else label.lower()
    if classes:
        raise ValueError(f"Unsupported ordered list style: {classes}")
    return str(number)


def statement_part(part_id: str, nodes, source_url: str, label: str | None = None) -> dict:
    nodes = list(nodes)
    part = {"id": part_id, "name": "item" if label else "statement"}
    if label:
        part["props"] = [{"name": "label", "value": label + "."}]
    lists = [index for index, node in enumerate(nodes) if isinstance(node, Tag) and node.name == "ol"]
    if not lists:
        text = prose(nodes, source_url)
        if not text:
            raise ValueError(f"Empty statement item: {part_id}")
        part["prose"] = text
        return part
    first, last = lists[0], lists[-1]
    if any(
        not is_blank(node) for node in nodes[first:]
        if not (isinstance(node, Tag) and node.name == "ol")
    ):
        raise ValueError(f"Unsupported statement structure after list: {part_id}")
    text = prose(nodes[:first], source_url)
    if text:
        part["prose"] = text
    part["parts"] = []
    for node in nodes[first:last + 1]:
        if not isinstance(node, Tag):
            continue
        start = int(node.get("start", 1))
        for offset, item in enumerate(node.find_all("li", recursive=False)):
            item_label = list_label(node, start + offset)
            part["parts"].append(
                statement_part(f"{part_id}.{item_label.lower()}", item.children, source_url, item_label)
            )
    item_ids = [child["id"] for child in part["parts"]]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError(f"Duplicate statement item identifiers in {part_id}")
    return part


def statement_parts(part: dict):
    yield part
    for child in part.get("parts", []):
        yield from statement_parts(child)


def sections(nodes: list) -> dict[str, list]:
    result = {"preamble": []}
    current = "preamble"
    for node in nodes:
        if isinstance(node, Tag) and node.name == "h3":
            current = node.get_text(" ", strip=True).lower().rstrip(":")
            result.setdefault(current, [])
            continue
        if isinstance(node, Tag) and node.name == "p":
            label = node.find(["strong", "b"], recursive=False)
            if label:
                name = label.get_text(" ", strip=True).lower().rstrip(":")
                if name in ("control", "activity", "activity/control", "references"):
                    current = "control" if name == "activity/control" else name
                    node = copy.copy(node)
                    node.find(["strong", "b"], recursive=False).decompose()
        result.setdefault(current, []).append(node)
    return result


def enhancement_sections(item: Tag) -> tuple[str, str, dict[str, list]]:
    heading = item.find(["strong", "b"], recursive=False)
    if heading is None:
        raise ValueError(f"Enhancement has no heading: {item.get_text()[:100]}")
    match = re.fullmatch(r"\((\d+)\)\s*(.+)", heading.get_text(" ", strip=True))
    if not match:
        raise ValueError(f"Invalid enhancement heading: {heading.get_text()}")
    result = {"statement": []}
    current = "statement"
    body = item.find(["ul", "ol"], recursive=False)
    if body is None:
        remaining = [node for node in item.children if node is not heading]
        result["statement"] = remaining
    else:
        for child in body.find_all("li", recursive=False):
            child = copy.copy(child)
            label = child.find(["strong", "b"], recursive=False)
            if label is not None:
                section = label.get_text(" ", strip=True).lower().rstrip(":").strip()
                if section in ("discussion", "gc discussion", "related controls and activities", "references"):
                    current = section
                    label.decompose()
            result.setdefault(current, []).extend(child.contents)
            result[current].append(BeautifulSoup("<br/>", "html.parser").br)
    return match[1], match[2], result


def parameter_slots(text: str) -> list[str]:
    slots = []
    index = 0
    while index < len(text):
        match = re.search(r"\[(?:Assignment|Selection)\b", text[index:], re.IGNORECASE)
        if match is None:
            break
        start = index + match.start()
        depth = 1
        end = start + 1
        while end < len(text) and depth:
            depth += (text[end] == "[") - (text[end] == "]")
            end += 1
        if depth:
            raise ValueError(f"Unclosed parameter: {text[start:start + 150]}")
        slots.append(text[start:end])
        index = end
    return slots


def parameter_order(control: dict, all_params: dict) -> list[str]:
    statement = next((p for p in control.get("parts", []) if p["name"] == "statement"), {})
    ids = INSERT.findall(json.dumps(statement))
    result = []

    def collect(param_id: str, ancestors: set) -> None:
        if param_id in ancestors:
            raise ValueError(f"Cyclic NIST parameter: {param_id}")
        result.append(param_id)
        for nested in INSERT.findall(json.dumps(all_params[param_id].get("select", {}))):
            collect(nested, ancestors | {param_id})

    for param_id in ids:
        collect(param_id, set())
    return list(dict.fromkeys(result))


def normalize_label(text: str) -> str:
    text = re.sub(r"organization[- ]defined\s*", "", text, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def split_choices(text: str) -> list[str]:
    depth = 0
    start = 0
    choices = []
    for index, char in enumerate(text):
        depth += (char == "[") - (char == "]")
        if char == ";" and depth == 0:
            choices.append(text[start:index].strip())
            start = index + 1
    choices.append(text[start:].strip())
    return [choice for choice in choices if choice]


def apply_parameters(control: dict, original: dict, all_params: dict, mappings: dict, diagnostics: list) -> None:
    statement = next((p for p in control["parts"] if p["name"] == "statement"), None)
    if statement is None:
        return
    old_ids = parameter_order(original, all_params)
    old_slots = []
    for param_id in old_ids:
        param = all_params[param_id]
        old_slots.append((param_id, param))
    params = {}
    used = set()
    overrides = copy.deepcopy(mappings.get(control["id"], {}))
    counter = 0

    def replace_slot(slot: str) -> str:
        nonlocal counter
        counter += 1
        kind, label = slot[1:-1].split(":", 1)
        is_selection = kind.lower().startswith("selection")
        label = label.strip()
        key = re.sub(r"^organization[- ]defined\s*", "", label, flags=re.IGNORECASE)
        candidates = [
            (param_id, param)
            for param_id, param in old_slots
            if param_id not in used
            and (
                (is_selection and "select" in param)
                or (not is_selection and normalize_label(param.get("label", "")) == normalize_label(label))
            )
        ]
        if key in overrides:
            if not overrides[key]:
                raise ValueError(f"Too few explicit parameter mappings for {control['id']}: {key}")
            param_id = overrides[key].pop(0)
            if param_id not in all_params:
                raise ValueError(f"Unknown legacy parameter: {param_id}")
            used.add(param_id)
        elif candidates:
            param_id, _ = candidates[0]
            used.add(param_id)
        else:
            param_id = f"{control['id']}_cccs_odp.{counter:02}"
            diagnostics.append({"control-id": control["id"], "param-id": param_id, "source": slot})
        param = {"id": param_id}
        if is_selection:
            choices = split_choices(label)
            if not choices:
                raise ValueError(f"No selection choices in {control['id']}: {slot}")
            param["select"] = {"choice": [replace_text(choice) for choice in choices]}
            if re.search(r"more|multiple", kind, re.IGNORECASE):
                param["select"]["how-many"] = "one-or-more"
        else:
            param["label"] = re.sub(r"^organization[- ]defined\s*", "", label, flags=re.IGNORECASE)
        if param_id in params and param != params[param_id]:
            raise ValueError(f"Conflicting definitions for shared parameter: {param_id}")
        params[param_id] = param
        return f"{{{{ insert: param, {param_id} }}}}"

    def replace_text(value: str) -> str:
        for slot in parameter_slots(value):
            value = value.replace(slot, replace_slot(slot), 1)
        return value

    for part in statement_parts(statement):
        if "prose" in part:
            part["prose"] = replace_text(part["prose"])
    if any(overrides.values()):
        raise ValueError(f"Unused parameter mappings for {control['id']}: {overrides}")
    if params:
        control["params"] = list(params.values())


def make_control(cid: str, title: str, content: dict, href: str, activity: bool = False) -> dict:
    family, number = cid.split("-", 1)
    numbers = number.split(".")
    label = family.upper() + "-" + f"{int(numbers[0]):02}"
    if len(numbers) == 2:
        label += f"({int(numbers[1]):02})"
    result = {
        "id": cid,
        "class": "CCCS-enhancement" if "." in cid else "CCCS-activity" if activity else "CCCS-control",
        "title": title,
        "props": [
            {"name": "label", "value": label},
            {"name": "sort-id", "value": family + "-" + ".".join(f"{int(n):02}" for n in numbers)},
            {"name": "source", "ns": NS, "value": "ITSP.10.033"},
        ],
        "links": [{"href": href, "rel": "reference"}],
        "parts": [],
    }
    statement_nodes = content.get("control", content.get("activity", content.get("statement", [])))
    statement = prose(statement_nodes, href)
    preamble = prose(content.get("preamble", []), href)
    if re.match(r"[\s*\[]*withdrawn\b", statement or preamble, re.IGNORECASE):
        result["props"].append({"name": "status", "value": "withdrawn"})
        result["parts"].append({
            "id": cid + "_withdrawn", "name": "withdrawn", "ns": NS, "prose": statement or preamble,
        })
        relationship = "moved-to" if re.search(r"\bmoved\b", statement or preamble, re.IGNORECASE) else "incorporated-into"
        for match in CONTROL_LABEL.finditer(statement or preamble):
            result["links"].append({"href": "#" + control_id(match[0]), "rel": relationship})
    elif statement:
        result["parts"].append(statement_part(cid + "_smt", statement_nodes, href))
        if preamble:
            result["parts"].append({"id": cid + "_notes", "name": "source-notes", "ns": NS, "prose": preamble})
    else:
        raise ValueError(f"{cid} has no statement and is not explicitly withdrawn")
    guidance = []
    for heading in ("discussion", "gc discussion"):
        text = prose(content.get(heading, []), href)
        if text:
            guidance.append(("**GC discussion**\n\n" if heading == "gc discussion" else "") + text)
    if guidance:
        result["parts"].append({"id": cid + "_gdn", "name": "guidance", "prose": "\n\n".join(guidance)})
    elif not any(p.get("name") == "status" for p in result["props"]):
        raise ValueError(f"{cid} has no published discussion")
    for name in ("related controls and activities", "references"):
        nodes = content.get(name, [])
        text = prose(nodes, href)
        if text:
            result["parts"].append({
                "id": cid + "_" + ("related" if name.startswith("related") else "references"),
                "name": "related-controls" if name.startswith("related") else "references",
                "ns": NS,
                "prose": text,
            })
        if name.startswith("related"):
            for match in CONTROL_LABEL.finditer(text):
                target = control_id(match[0])
                link = {"href": "#" + target, "rel": "related"}
                if link not in result["links"]:
                    result["links"].append(link)
    known = {
        "preamble", "control", "activity", "statement", "discussion", "gc discussion",
        "related controls and activities", "references", "enhancements",
    }
    for index, (name, nodes) in enumerate(content.items()):
        if name not in known and prose(nodes, href):
            result["parts"].append({
                "id": f"{cid}_section.{index}",
                "name": "source-section",
                "ns": NS,
                "title": name,
                "prose": prose(nodes, href),
            })
    return result


def parse_family(family: str, body: str, href: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    controls = []
    for heading in soup.find_all("h2"):
        match = re.fullmatch(r"([A-Z]{2}-\d+)\s+(.+)", heading.get_text(" ", strip=True))
        if not match:
            continue
        cid = control_id(match[1])
        if not cid.startswith(family + "-"):
            raise ValueError(f"Unexpected family: {cid}")
        nodes = []
        for node in heading.next_siblings:
            if isinstance(node, Tag) and node.name == "h2":
                break
            nodes.append(node)
        content = sections(nodes)
        source = href + "#" + heading["id"]
        control = make_control(cid, match[2], content, source, "activity" in content)
        enhancements = []
        for node in content.get("enhancements", []):
            if not isinstance(node, Tag):
                continue
            if node.name in ("ul", "ol"):
                for item in node.find_all("li", recursive=False):
                    number, title, details = enhancement_sections(item)
                    enhancements.append(make_control(f"{cid}.{int(number)}", title, details, source))
        if enhancements:
            control["controls"] = enhancements
        controls.append(control)
    if not controls:
        raise ValueError(f"No controls parsed from {href}")
    enhancement_count = sum(
        bool(re.match(r"^\(\d+\)\s*\S", heading.get_text(" ", strip=True)))
        for heading in soup.find_all(["strong", "b"])
    )
    if sum(len(c.get("controls", [])) for c in controls) != enhancement_count:
        raise ValueError(f"Unparsed enhancement headings in {href}")
    return {"id": family, "title": FAMILIES[family].replace("-", " ").capitalize(), "controls": controls}


def metadata(title: str) -> dict:
    return {
        "title": title,
        "last-modified": MODIFIED,
        "version": VERSION,
        "oscal-version": OSCAL_VERSION,
    }


def write_json(path: Path, value: dict, check: bool) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if check:
        if path.read_text(encoding="utf-8") != text:
            raise ValueError(f"Generated content is out of date: {path}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def parse_requirements(body: str, diagnostics: list) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    groups = {}
    for heading in soup.find_all("h4"):
        match = re.fullmatch(r"(03\.\d{2}\.\d{2})\s+(.+)", heading.get_text(" ", strip=True))
        if not match:
            continue
        label, title = match.groups()
        details = heading.find_parent("details")
        if details is None:
            nodes = []
            for node in heading.next_siblings:
                if isinstance(node, Tag) and node.name in ("h2", "h3", "h4", "details"):
                    break
                nodes.append(node)
        else:
            nodes = list(details.children)
        content = {"statement": []}
        current = "statement"
        for node in nodes:
            if isinstance(node, Tag) and node.name == "summary":
                continue
            if isinstance(node, Tag) and node.name == "h5":
                current = node.get_text(" ", strip=True).lower().rstrip(":")
                content.setdefault(current, [])
                continue
            content.setdefault(current, []).append(node)
        cid = "itsp171-" + label
        control = {
            "id": cid,
            "class": "CCCS-requirement",
            "title": title,
            "props": [
                {"name": "label", "value": label},
                {"name": "sort-id", "value": cid},
                {"name": "source", "ns": NS, "value": "ITSP.10.171"},
            ],
            "links": [{"href": BASE + ITSP171 + "#" + heading["id"], "rel": "reference"}],
            "parts": [],
        }
        statement_nodes = content.pop("statement")
        statement = prose(statement_nodes, BASE + ITSP171)
        withdrawn = re.match(r"[\s*\[]*withdrawn\b", statement, re.IGNORECASE)
        if not statement:
            raise ValueError(f"Empty requirement: {label}")
        if withdrawn:
            control["props"].append({"name": "status", "value": "withdrawn"})
            control["parts"].append({"id": cid + "_withdrawn", "name": "withdrawn", "ns": NS, "prose": statement})
        else:
            control["parts"].append(statement_part(cid + "_smt", statement_nodes, BASE + ITSP171))
            if not prose(content.get("discussion", []), BASE + ITSP171):
                raise ValueError(f"No discussion for requirement {label}")
        for name, nodes in content.items():
            text = prose(nodes, BASE + ITSP171)
            if text:
                part = {"id": cid + "_" + name.replace(" ", "-"), "name": "guidance", "prose": text}
                if name != "discussion":
                    part.update({"name": "source-section", "ns": NS, "title": name})
                control["parts"].append(part)
        apply_parameters(control, {}, {}, {}, diagnostics)
        group_id = label.rsplit(".", 1)[0]
        if group_id not in groups:
            family_heading = heading.find_previous("h3")
            groups[group_id] = {
                "id": "itsp171-" + group_id,
                "title": family_heading.get_text(" ", strip=True),
                "controls": [],
            }
        groups[group_id]["controls"].append(control)
    if len(groups) != 17:
        raise ValueError(f"Expected 17 ITSP.10.171 requirement families, found {len(groups)}")
    catalog = {
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, NS + "/itsp171/catalog/" + VERSION)),
        "metadata": metadata("CCCS Protecting specified information in non-Government of Canada systems and organizations (ITSP.10.171)"),
        "groups": list(groups.values()),
    }
    catalog["metadata"]["links"] = [{"href": BASE + ITSP171, "rel": "reference"}]
    catalog["metadata"]["remarks"] = (
        "Unofficial OSCAL transcription of the Canadian Centre for Cyber Security's "
        "Protecting specified information in non-Government of Canada systems and organizations "
        "(ITSP.10.171), second release, October 28, 2025. Source text is Crown copyright, "
        "reproduced under the Government of Canada non-commercial reproduction terms, not "
        "the repository's MIT-0 software license. Commercial redistribution requires permission. "
        "Organization-defined parameters remain unassigned unless the source specifies values. "
        "This transcription is not endorsed by the Government of Canada."
    )
    controls = {c["id"]: c for c in walk_controls(catalog)}
    for row in soup.find("table", id="tab21").select("tbody > tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 2:
            raise ValueError("Unexpected Annex B parameter row")
        anchor = cells[0].find("a")
        if anchor is None:
            raise ValueError("Annex B parameter has no requirement reference")
        cid = "itsp171-" + anchor["href"].lstrip("#").replace("-", ".")
        if cid not in controls:
            raise ValueError(f"Unknown Annex B requirement: {cid}")
        part = next((p for p in controls[cid]["parts"] if p["name"] == "odp-reference"), None)
        if part is None:
            part = {"id": cid + "_odp-reference", "name": "odp-reference", "ns": NS, "prose": ""}
            controls[cid]["parts"].append(part)
        part["prose"] += f"- {cells[0].get_text(' ', strip=True)}: {cells[1].get_text(' ', strip=True)}\n"
    return catalog


def update_profiles(pages: dict) -> tuple[dict, dict]:
    path = ROOT / "imports" / "cccs-mods-profile" / "cccs-mods-profile.json"
    profile = {
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, NS + "/profiles/cccs-mods/" + VERSION)),
        "metadata": metadata("CCCS ITSP.10.033 catalog compatibility profile"),
        "imports": [{"href": "../cccs-control-catalog/cccs-control-catalog.json", "include-all": {}}],
        "merge": {"as-is": True},
    }
    profile["metadata"]["remarks"] = (
        "Compatibility entry point for existing consumers. Imports the complete authoritative "
        "CCCS catalog, rather than altering NIST controls. Titles, statements, discussions and "
        "Canadian-specific controls now come directly from published ITSP.10.033. "
        "No SI-3(9) workaround or NIST catalog import is required."
    )
    profile["metadata"]["remarks"] += "\n\n" + COPYRIGHT
    profile["metadata"]["links"] = [{"href": BASE + GUIDANCE, "rel": "reference"}]
    documents = {path: {"profile": profile}}
    coverage = {}
    for path in sorted((ROOT / "profiles").glob("*/*-profile.json")):
        profile = json.loads(path.read_text(encoding="utf-8"))["profile"]
        slug = path.parent.name
        profile["uuid"] = str(uuid.uuid5(uuid.NAMESPACE_URL, NS + "/profiles/" + slug + "/" + VERSION))
        profile["metadata"].update(metadata(profile["metadata"]["title"]))
        profile.pop("back-matter", None)
        if slug == "cccs-itsp.10.033-01-profile":
            soup = BeautifulSoup(pages["medium"], "html.parser")
            selected = []
            alters = []
            allocation_counts = {}
            for table in soup.find_all("table"):
                caption = table.find("caption")
                if caption is None or not caption.get("id", "").startswith("tab4."):
                    continue
                for row in table.select("tbody > tr"):
                    cells = row.find_all("td", recursive=False)
                    if len(cells) != 8:
                        raise ValueError("Unexpected ITSP.10.033-01 control table row")
                    allocation = cells[5].get_text(" ", strip=True)
                    allocation_counts[allocation] = allocation_counts.get(allocation, 0) + 1
                    if allocation not in ("Selected", "Deployed organization-wide. Not associated with baseline."):
                        if allocation not in ("NA", "Not selected", "Not allocated to baseline."):
                            raise ValueError(f"Unknown medium profile allocation: {allocation}")
                        continue
                    cid = control_id(cells[0].get_text(strip=True) + "-" + cells[1].get_text(strip=True))
                    selected.append(cid)
                    text = "**Published profile description**\n\n" + prose([cells[3]], BASE + MEDIUM)
                    for title, cell in (("Suggested placeholder values", cells[6]), ("Profile-specific notes", cells[7])):
                        value = prose([cell], BASE + MEDIUM)
                        if value not in ("NA", "N/A", ""):
                            text += f"\n\n**{title}**\n\n{value}"
                    alters.append({
                        "control-id": cid,
                        "adds": [{
                            "props": [{"name": "profile-allocation", "ns": NS, "value": allocation}],
                            "links": [{"href": BASE + MEDIUM + "#" + caption["id"], "rel": "reference"}],
                            "parts": [{
                                "id": cid + "_medium-profile", "name": "profile-guidance", "ns": NS,
                                "title": "ITSP.10.033-01 profile description and tailoring guidance", "prose": text,
                            }],
                        }],
                    })
            profile["imports"][0]["include-controls"] = [{"with-ids": selected}]
            profile["modify"] = {"alters": alters}
            profile["metadata"]["links"] = [{"href": BASE + MEDIUM, "rel": "reference"}]
            profile["metadata"]["remarks"] = (
                "April 2, 2026 release. Includes every Selected entry and all organization-wide "
                "PM controls and activities. PT entries are not allocated to the baseline by the "
                "publication; they remain available in the complete CCCS catalog for tailoring. "
                "Published profile descriptions, suggested placeholder values and profile-specific "
                "notes are retained as profile guidance. Suggestions are not silently interpreted "
                "as machine-readable assignments when the source does not identify a unique parameter."
            )
            coverage["medium"] = allocation_counts
        elif slug == "cccs-itsp.10.171-profile":
            soup = BeautifulSoup(pages["itsp171"], "html.parser")
            selected = []
            alters = []
            criteria_counts = {}
            for table in soup.find_all("table", id=re.compile(r"tab(?:[1-9]|1\d|20)$")):
                for row in table.select("tbody > tr"):
                    cells = row.find_all("td", recursive=False)
                    if len(cells) != 4:
                        raise ValueError("Unexpected ITSP.10.171 Annex A row")
                    criteria = cells[2].get_text(" ", strip=True)
                    criteria_counts[criteria] = criteria_counts.get(criteria, 0) + 1
                    if criteria != "C":
                        continue
                    cid = control_id(cells[0].get_text(strip=True))
                    selected.append(cid)
                    links = []
                    for anchor in cells[3].find_all("a"):
                        target = anchor["href"].lstrip("#").replace("-", ".")
                        links.append({"href": "#itsp171-" + target, "rel": "related"})
                    alters.append({
                        "control-id": cid,
                        "adds": [{
                            "props": [{"name": "tailoring-criteria", "ns": NS, "value": "C"}],
                            "links": links,
                            "parts": [{
                                "id": cid + "_itsp171-mapping", "name": "profile-guidance", "ns": NS,
                                "prose": "**ITSP.10.171 Annex A security requirement mapping**\n\n" + prose([cells[3]], BASE + ITSP171),
                            }],
                        }],
                    })
            profile["imports"] = [
                {
                    "href": "../../imports/cccs-itsp.10.171-catalog/cccs-itsp.10.171-catalog.json",
                    "include-all": {},
                },
                {
                    "href": "../../imports/cccs-mods-profile/cccs-mods-profile.json",
                    "include-controls": [{"with-ids": selected}],
                },
            ]
            profile["modify"] = {"alters": alters}
            profile["metadata"]["links"] = [{"href": BASE + ITSP171, "rel": "reference"}]
            profile["metadata"]["remarks"] = (
                "October 28, 2025 second release. Includes the actual numbered ITSP.10.171 "
                "requirements, discussions and Annex B parameter inventory, as well as every "
                "Annex A source control marked C. The source-control crosswalk is supporting "
                "context, not a substitute for the numbered requirements or their tailoring. "
                "Organization-defined parameters require organization-specific decisions."
            )
            profile["metadata"]["remarks"] += (
                " Requirement text: Canadian Centre for Cyber Security, Protecting specified "
                "information in non-Government of Canada systems and organizations (ITSP.10.171), "
                "October 28, 2025; an unofficial reproduction of the publication at the reference URL."
            )
            coverage["itsp171"] = criteria_counts
        else:
            assignments = {}
            for assignment in profile.get("modify", {}).get("set-parameters", []):
                param_id = assignment["param-id"]
                if param_id in assignments and assignments[param_id] != assignment:
                    raise ValueError(f"Conflicting assignments in {slug}: {param_id}")
                assignments[param_id] = assignment
            if assignments:
                profile["modify"]["set-parameters"] = list(assignments.values())
            profile["metadata"]["remarks"] = (
                "Legacy sample profile selections and parameter assignments are preserved. "
                "Control titles, statements and discussion now use the published March 31, 2026 "
                "CCCS ITSP.10.033 catalog. This is a versioned sample, not a certification that "
                "the cloud/FedRAMP/PBHVA allocations reflect a newly issued baseline. "
                "Review applicability and organization-defined values before operational use."
            )
        if not profile.get("modify"):
            profile.pop("modify", None)
        profile["metadata"]["remarks"] += "\n\n" + COPYRIGHT
        references = profile["metadata"].setdefault("links", [])
        catalog_reference = {"href": BASE + GUIDANCE, "rel": "reference"}
        if catalog_reference not in references:
            references.append(catalog_reference)
        documents[path] = {"profile": profile}
    return coverage, documents


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CCCS OSCAL content from authoritative publications.")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".cache" / "cccs-sources")
    parser.add_argument("--update-lock", action="store_true", help="Explicitly accept changed source publications.")
    parser.add_argument("--refresh", action="store_true", help="Re-download official source pages.")
    parser.add_argument("--check", action="store_true", help="Check generated source content without writing it.")
    args = parser.parse_args()
    if args.check and args.update_lock:
        parser.error("--check and --update-lock cannot be used together")
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    expected = json.loads(LOCK.read_text(encoding="utf-8")) if LOCK.exists() else {}
    if not expected and not args.update_lock:
        parser.error("Source lock is missing; review sources and use --update-lock")
    sources = {}
    groups = []
    pages = {}
    for family, slug in FAMILIES.items():
        source, body = source_page(f"{GUIDANCE}/{slug}", args.cache_dir, args.refresh)
        sources[family] = source
        if not args.update_lock and source != expected["sources"][family]:
            raise ValueError(f"Source changed: {source['url']}; review before updating the source lock")
        group = parse_family(family, body, source["url"])
        group["title"] = source["title"]
        groups.append(group)
        print(f"{family.upper()}: {len(list(walk_controls(group)))} controls and enhancements", flush=True)
    for name, path in (("medium", MEDIUM), ("itsp171", ITSP171)):
        source, body = source_page(path, args.cache_dir, args.refresh)
        sources[name] = source
        pages[name] = body
        if not args.update_lock and source != expected["sources"][name]:
            raise ValueError(f"Source changed: {source['url']}")
    nist_cache = args.cache_dir / "nist-catalog.json"
    if not nist_cache.exists():
        nist_cache.write_bytes(download(NIST_URL))
    nist_bytes = nist_cache.read_bytes()
    sources["nist-parameter-identifiers"] = {
        "url": NIST_URL, "commit": NIST_COMMIT, "sha256": hashlib.sha256(nist_bytes).hexdigest(),
    }
    if not args.update_lock and sources["nist-parameter-identifiers"] != expected["sources"]["nist-parameter-identifiers"]:
        raise ValueError("Pinned NIST parameter source checksum mismatch")
    originals = {c["id"]: c for c in walk_controls(json.loads(nist_bytes)["catalog"])}
    all_params = {p["id"]: p for c in originals.values() for p in c.get("params", [])}
    diagnostics = []
    catalog = {
        "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, NS + "/catalog/" + VERSION)),
        "metadata": metadata("CCCS Security and privacy controls and assurance activities catalogue (ITSP.10.033)"),
        "groups": groups,
    }
    catalog["metadata"]["links"] = [{"href": BASE + GUIDANCE, "rel": "reference"}]
    catalog["metadata"]["remarks"] = (
        "Unofficial OSCAL transcription of the Canadian Centre for Cyber Security's "
        "Security and privacy controls and assurance activities catalogue (ITSP.10.033), "
        "published March 31, 2026. Includes general and Government of Canada discussion. "
        "Source text is Crown copyright, reproduced under the Government of Canada "
        "non-commercial reproduction terms; it is not covered by this repository's MIT-0 "
        "software license. Commercial redistribution requires permission. "
        "This transcription is not endorsed by the Government of Canada."
    )
    corrections = json.loads(CATALOG.with_name("source-corrections.json").read_text(encoding="utf-8"))
    mappings = json.loads(CATALOG.with_name("parameter-mappings.json").read_text(encoding="utf-8"))
    for control in walk_controls(catalog):
        for correction in corrections:
            if correction["control-id"] == control["id"]:
                statement = next(p for p in control["parts"] if p["name"] == "statement")
                targets = [
                    part for part in statement_parts(statement)
                    if correction["before"] in part.get("prose", "")
                ]
                if len(targets) != 1 or targets[0]["prose"].count(correction["before"]) != 1:
                    raise ValueError(f"Source correction no longer applies to {control['id']}")
                targets[0]["prose"] = targets[0]["prose"].replace(correction["before"], correction["after"])
                control["props"].append({
                    "name": "source-correction", "ns": NS, "value": correction["reason"],
                })
        apply_parameters(control, originals.get(control["id"], {}), all_params, mappings, diagnostics)
    controls = {c["id"]: c for c in walk_controls(catalog)}
    parameter_owners = {}
    for control in controls.values():
        for param in control.get("params", []):
            parameter_owners.setdefault(param["id"], []).append(control)
    for param_id, owners in parameter_owners.items():
        if len(owners) < 2:
            continue
        parent_ids = {owner["id"].split(".")[0] for owner in owners}
        definitions = [next(p for p in owner["params"] if p["id"] == param_id) for owner in owners]
        if len(parent_ids) != 1 or any(p != definitions[0] for p in definitions):
            raise ValueError(f"Incompatible shared parameter: {param_id}")
        for owner in owners:
            owner["params"] = [p for p in owner["params"] if p["id"] != param_id]
            if not owner["params"]:
                del owner["params"]
        controls[next(iter(parent_ids))].setdefault("params", []).append(definitions[0])
    requirements = parse_requirements(pages["itsp171"], diagnostics)
    coverage, documents = update_profiles(pages)
    lock = {
        "sources": sources,
        "counts": {g["id"]: len(list(walk_controls(g))) for g in groups},
        "cccs-parameters": diagnostics,
        "itsp171-requirements": len(list(walk_controls(requirements))),
        "profile-coverage": coverage,
    }
    if not args.update_lock and lock != expected:
        raise ValueError("Source coverage or parameter mapping changed")
    documents[CATALOG] = {"catalog": catalog}
    documents[REQUIREMENTS_CATALOG] = {"catalog": requirements}
    if args.update_lock:
        documents[LOCK] = lock
    for path, document in documents.items():
        write_json(path, document, args.check)
    print(f"Built {sum(lock['counts'].values())} CCCS controls and enhancements.")


if __name__ == "__main__":
    main()
