import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load_script("build-catalog")
EXPORT = load_script("catalog-to-csv")
RESOLVE = load_script("resolve")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def active(control: dict) -> bool:
    return not any(p["name"] == "status" and p["value"] == "withdrawn" for p in control.get("props", []))


def statement_text(control: dict) -> str:
    statement = next(p for p in control["parts"] if p["name"] == "statement")
    return "\n".join(part.get("prose", "") for part in BUILD.statement_parts(statement))


def sample_control(cid: str = "ac-1") -> dict:
    return {
        "id": cid,
        "title": "Test control",
        "props": [{"name": "sort-id", "value": cid}],
        "parts": [
            {"id": cid + "_smt", "name": "statement", "prose": "A complete control statement."},
            {"id": cid + "_gdn", "name": "guidance", "prose": "General guidance.\n\n**GC discussion**\n\nCanadian guidance."},
        ],
    }


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = read(BUILD.CATALOG)["catalog"]
        cls.requirements = read(BUILD.REQUIREMENTS_CATALOG)["catalog"]
        cls.controls = {c["id"]: c for c in BUILD.walk_controls(cls.catalog)}

    def test_exact_published_coverage(self):
        lock = read(BUILD.LOCK)
        self.assertEqual(len(self.controls), 1248)
        self.assertEqual(len(self.catalog["groups"]), 20)
        self.assertEqual(sum(active(c) for c in self.controls.values()), 1059)
        self.assertEqual(
            {g["id"]: len(list(BUILD.walk_controls(g))) for g in self.catalog["groups"]},
            lock["counts"],
        )
        requirements = list(BUILD.walk_controls(self.requirements))
        self.assertEqual(len(requirements), 131)
        self.assertEqual(sum(map(active, requirements)), 99)
        self.assertEqual(len(self.requirements["groups"]), 17)

    def test_all_active_controls_have_published_statements_and_discussion(self):
        for catalog in (self.catalog, self.requirements):
            for control in BUILD.walk_controls(catalog):
                if not active(control):
                    continue
                with self.subTest(control=control["id"]):
                    parts = {p["name"]: p for p in control["parts"]}
                    self.assertTrue(statement_text(control).strip())
                    self.assertTrue(parts["guidance"]["prose"].strip())
                    self.assertNotRegex(json.dumps(control), r"\b(?:TBD|TODO|FIXME)\b")
                    self.assertFalse(BUILD.parameter_slots(statement_text(control)))

    def test_statement_items_keep_published_labels(self):
        expected = {
            "ac-2": ["A.", "B.", "C.", "D.", "E.", "F.", "G.", "H.", "I.", "J.", "K.", "L."],
            "cp-2": ["A.", "B.", "C.", "D.", "E.", "F.", "G.", "H.", "AA."],
            "pm-20": ["A.", "B.", "C.", "AA.", "BB."],
            "ma-4.5": ["a.", "b."],
        }
        for cid, labels in expected.items():
            with self.subTest(control=cid):
                statement = next(p for p in self.controls[cid]["parts"] if p["name"] == "statement")
                self.assertEqual([p["props"][0]["value"] for p in statement["parts"]], labels)
        item = next(p for p in BUILD.statement_parts(self.controls["ac-2"]["parts"][0]) if p["id"] == "ac-2_smt.d.1")
        self.assertEqual(item["name"], "item")
        self.assertEqual(item["prose"], "authorized users of the system")
        self.assertIn("cp-2_smt.aa", {p["id"] for p in BUILD.statement_parts(self.controls["cp-2"]["parts"][0])})

    def test_statement_lists_are_items_not_flattened_markdown(self):
        for catalog in (self.catalog, self.requirements):
            for control in BUILD.walk_controls(catalog):
                for statement in (p for p in control["parts"] if p["name"] == "statement"):
                    for part in BUILD.statement_parts(statement):
                        with self.subTest(part=part["id"]):
                            self.assertNotRegex(part.get("prose", ""), r"(?m)^\s*(?:\d+|[A-Za-z]{1,2})\.\s")

    def test_source_correction_applies_to_one_statement_item(self):
        item = next(p for p in BUILD.statement_parts(self.controls["ma-4.5"]["parts"][0]) if p["id"] == "ma-4.5_smt.a")
        self.assertRegex(item["prose"], r"by \{\{ insert: param, [^}]+ \}\}$")
        self.assertIn({"name": "source-correction", "ns": BUILD.NS, "value": read(BUILD.CATALOG.with_name("source-corrections.json"))[0]["reason"]}, self.controls["ma-4.5"]["props"])

    def test_identifiers_and_properties_are_unique(self):
        for catalog in (self.catalog, self.requirements):
            seen = set()
            for container in EXPORT.walk(catalog):
                parts = [part for top in container.get("parts", []) for part in BUILD.statement_parts(top)]
                for item in [container] + container.get("params", []) + parts:
                    if "id" in item:
                        self.assertNotIn(item["id"], seen)
                        seen.add(item["id"])
                    props = [(p["name"], p.get("ns"), p.get("class")) for p in item.get("props", [])]
                    self.assertEqual(len(props), len(set(props)))

    def test_parameter_references_are_resolvable_and_acyclic(self):
        for catalog in (self.catalog, self.requirements):
            params = {p["id"]: p for c in EXPORT.walk(catalog) for p in c.get("params", [])}
            for c in EXPORT.walk(catalog):
                for part in c.get("parts", []):
                    EXPORT.part_text(part, params)
            for param_id in params:
                EXPORT.render_text(f"{{{{ insert: param, {param_id} }}}}", params)

    def test_withdrawn_si39_is_retained_without_the_nist_duplicate(self):
        control = self.controls["si-3.9"]
        self.assertFalse(active(control))
        self.assertIn({"href": "#ac-17.10", "rel": "moved-to"}, control["links"])
        self.assertEqual(sum(p["name"] == "label" for p in control["props"]), 1)
        profile = read(ROOT / "imports" / "cccs-mods-profile" / "cccs-mods-profile.json")["profile"]
        self.assertEqual(len(profile["imports"]), 1)
        self.assertNotIn("exclude-controls", profile["imports"][0])
        self.assertNotIn("modify", profile)

    def test_previously_missing_unique_controls_have_content(self):
        for cid in ("ac-21.400", "ac-21.401", "pe-2.400", "pe-3.400", "pe-13.400", "pe-400", "pe-400.1", "pe-400.2", "pe-401", "pt-4.400", "pt-7.400"):
            with self.subTest(control=cid):
                self.assertTrue(active(self.controls[cid]))
                self.assertTrue(statement_text(self.controls[cid]).strip())

    def test_modified_titles_are_canadian_definitions_not_nist_titles(self):
        self.assertEqual(self.controls["pt-2"]["title"], "Authority to collect and use personal information")
        self.assertEqual(self.controls["pt-3"]["title"], "Personal information handling uses and disclosures")
        self.assertEqual(self.controls["pt-2"]["class"], "CCCS-control")

    def test_annex_b_inventory_is_retained(self):
        inventory = [p for c in BUILD.walk_controls(self.requirements) for p in c["parts"] if p["name"] == "odp-reference"]
        self.assertTrue(inventory)
        self.assertIn(".F.02", inventory[0]["prose"])

    def test_crown_copyright_is_not_relicensed_as_mit(self):
        for catalog in (self.catalog, self.requirements):
            self.assertIn("non-commercial", catalog["metadata"]["remarks"])
            self.assertIn("Commercial redistribution requires permission", catalog["metadata"]["remarks"])


class ProfileTests(unittest.TestCase):
    LEGACY_HASHES = {
        "cccs-cloud-medium-profile": (
            "1a4807f1083694fbe51dabd4d46c7cef03298a0c6d7942a0bfbe3a8d3db8db10",
            "5bae2aead8319029dda3f64f4f3bbb0649890acc0fd159fe2f45917e4c9a7596",
        ),
        "cccs-medium+pbhva-profile": (
            "55cb0706154990e33291dbdcddf2191dba3f2b4c85733e337a8b70e6d84b8b74",
            "8c85c4fb5b35c33f64fa905c9769bad0fb462581380c4be055d62463c6f432d8",
        ),
        "cccs-pbhva-overlay-profile": (
            "a1f059bec72c3a539d8af8547d68bd422ceb660b979cbafacdf4094f24a22c6f",
            "450210af52bf33bb84a786ead1fa890f3b799f50be8773d29138f44316e7109d",
        ),
        "cccs-saas-fedramp-profile": (
            "07e808f9dbb23f84e03da0729cae0c618e82b2d5afd17b217fbf616aadff548f",
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        ),
    }

    def test_legacy_selections_and_all_assignments_are_preserved(self):
        for name, hashes in self.LEGACY_HASHES.items():
            profile = read(ROOT / "profiles" / name / f"{name}.json")["profile"]
            with self.subTest(profile=name):
                self.assertEqual(digest(profile["imports"]), hashes[0])
                self.assertEqual(digest(profile.get("modify", {}).get("set-parameters", [])), hashes[1])

    def test_official_medium_selections_and_source_guidance(self):
        profile = read(ROOT / "profiles" / "cccs-itsp.10.033-01-profile" / "cccs-itsp.10.033-01-profile.json")["profile"]
        selected = profile["imports"][0]["include-controls"][0]["with-ids"]
        self.assertEqual(len(selected), 417)
        self.assertEqual(sum(cid.startswith("pm-") for cid in selected), 37)
        self.assertFalse(any(cid.startswith("pt-") for cid in selected))
        self.assertEqual({a["control-id"] for a in profile["modify"]["alters"]}, set(selected))
        ac1 = next(a for a in profile["modify"]["alters"] if a["control-id"] == "ac-1")
        self.assertIn("at a frequency no longer than annually", ac1["adds"][0]["parts"][0]["prose"])

    def test_itsp171_has_real_requirements_and_the_complete_crosswalk(self):
        profile = read(ROOT / "profiles" / "cccs-itsp.10.171-profile" / "cccs-itsp.10.171-profile.json")["profile"]
        self.assertIn("cccs-itsp.10.171-catalog", profile["imports"][0]["href"])
        self.assertIn("include-all", profile["imports"][0])
        self.assertEqual(len(profile["imports"][1]["include-controls"][0]["with-ids"]), 157)
        self.assertEqual(len(profile["modify"]["alters"]), 157)

    def test_document_uuids_are_distinct(self):
        paths = [BUILD.CATALOG, BUILD.REQUIREMENTS_CATALOG] + RESOLVE.PROFILE_PATHS
        uuids = [next(iter(read(path).values()))["uuid"] for path in paths]
        self.assertEqual(len(uuids), len(set(uuids)))

    def test_every_profile_import_is_a_local_existing_resource(self):
        for path in RESOLVE.PROFILE_PATHS:
            for imp in read(path)["profile"]["imports"]:
                self.assertNotIn("://", imp["href"])
                self.assertTrue((path.parent / imp["href"]).resolve().is_file())

    def test_comparison_notebook_handles_the_additional_requirements_import(self):
        notebook = read(ROOT / "notebooks" / "compare-profiles.ipynb")
        source = next("".join(c["source"]) for c in notebook["cells"] if "class profile(dict):" in "".join(c["source"]))
        namespace = {}
        exec(compile(source, "compare-profiles.ipynb", "exec"), namespace)
        profile = read(ROOT / "profiles" / "cccs-itsp.10.171-profile" / "cccs-itsp.10.171-profile.json")
        self.assertEqual(len(namespace["profile"](profile).controls), 157)

    def test_comparison_notebook_reads_unicode_profiles_as_utf8(self):
        notebook = read(ROOT / "notebooks" / "compare-profiles.ipynb")
        source = next("".join(c["source"]) for c in notebook["cells"] if "# Read CCCS profiles" in "".join(c["source"]))
        source = source.split("# Read online profiles", 1)[0]
        namespace = {"os": os, "json": json, "profiles_path": str(ROOT / "profiles")}
        with patch("builtins.open", wraps=open) as files:
            exec(compile(source, "compare-profiles.ipynb", "exec"), namespace)
        self.assertEqual(len(namespace["profile_data"]), 6)
        self.assertTrue(all(call.kwargs.get("encoding") == "utf-8" for call in files.call_args_list))


class ParserTests(unittest.TestCase):
    def test_nested_selection_and_deeper_assignments_are_parameters(self):
        control = sample_control()
        control["parts"][0]["prose"] = (
            "[Selection (one or more): use [Assignment: organization-defined tools]; "
            "[Selection (1): manual; automated]]"
        )
        diagnostics = []
        BUILD.apply_parameters(control, {}, {}, {}, diagnostics)
        params = {p["id"]: p for p in control["params"]}
        self.assertEqual(len(params), 3)
        text = EXPORT.render_text(control["parts"][0]["prose"], params)
        self.assertIn("[Assignment: organization-defined tools]", text)
        self.assertIn("[Selection: manual; automated]", text)
        self.assertIn("Selection (one or more)", text)
        self.assertNotIn("{{", text)

    def test_nested_choice_separators_are_not_split(self):
        self.assertEqual(BUILD.split_choices("a [Selection: b; c]; d"), ["a [Selection: b; c]", "d"])

    def test_unclosed_source_parameter_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Unclosed parameter"):
            BUILD.parameter_slots("[Assignment: organization-defined missing bracket")

    def test_unused_explicit_mapping_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Unused parameter mappings"):
            BUILD.apply_parameters(sample_control(), {}, {}, {"ac-1": {"missing": ["old"]}}, [])

    def test_relative_citations_point_to_the_publication_not_the_site_root(self):
        soup = BUILD.BeautifulSoup('<p>See <a href="#section">section</a>.</p>', "html.parser")
        self.assertIn("(https://www.cyber.gc.ca/en/example#section)", BUILD.prose([soup], "https://www.cyber.gc.ca/en/example"))

    def test_activity_control_and_gc_discussion_are_preserved(self):
        html = (
            '<h2 id="test">AC-12 Session termination</h2>'
            '<p><strong>Activity/Control:</strong> End sessions.</p>'
            '<h3>Discussion</h3><p>General discussion.</p>'
            '<h3>GC discussion</h3><p>Canadian discussion.</p>'
            '<h3>Related controls and activities</h3><p>AC-02.</p>'
        )
        control = BUILD.parse_family("ac", html, "https://www.cyber.gc.ca/test")["controls"][0]
        self.assertEqual(control["parts"][0]["prose"], "End sessions.")
        self.assertIn("Canadian discussion.", control["parts"][1]["prose"])
        self.assertIn({"href": "#ac-2", "rel": "related"}, control["links"])

    def test_withdrawn_control_is_not_a_fabricated_statement(self):
        html = '<h2 id="test">AC-13 Retired</h2><p>Withdrawn: Incorporated into AC-02.</p>'
        control = BUILD.parse_family("ac", html, "https://www.cyber.gc.ca/test")["controls"][0]
        self.assertFalse(active(control))
        self.assertFalse(any(p["name"] == "statement" for p in control["parts"]))

    def test_source_control_without_a_statement_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "no statement"):
            BUILD.parse_family("ac", '<h2 id="test">AC-01 Incomplete</h2>', "https://www.cyber.gc.ca/test")

    def test_ordered_lists_become_labelled_statement_items(self):
        html = (
            '<h2 id="test">AC-02 Accounts</h2><h3>Control</h3>'
            '<ol class="lst-upr-alph"><li>Specify<ol><li>users [Assignment: organization-defined roles]</li></ol></li></ol>'
            '<ol class="lst-upr-alph" start="27"><li>Canadian addition</li></ol>'
            '<h3>Discussion</h3><p>Guidance.</p>'
        )
        control = BUILD.parse_family("ac", html, "https://www.cyber.gc.ca/test")["controls"][0]
        BUILD.apply_parameters(control, {}, {}, {}, [])
        statement = control["parts"][0]
        self.assertNotIn("prose", statement)
        self.assertEqual([p["id"] for p in BUILD.statement_parts(statement)], [
            "ac-2_smt", "ac-2_smt.a", "ac-2_smt.a.1", "ac-2_smt.aa",
        ])
        self.assertEqual(statement["parts"][1]["props"], [{"name": "label", "value": "AA."}])
        self.assertEqual(statement["parts"][0]["parts"][0]["prose"], "users {{ insert: param, ac-2_cccs_odp.01 }}")
        rows = EXPORT.catalog_to_rows({"groups": [{"id": "ac", "controls": [control]}]})
        self.assertEqual([row[2] for row in rows], ["", "A", "A.1", "AA"])

    def test_text_after_a_statement_list_fails_explicitly(self):
        html = (
            '<h2 id="test">AC-02 Accounts</h2><h3>Control</h3>'
            '<ol class="lst-upr-alph"><li>Item</li></ol><p>Unplaceable trailing text.</p>'
            '<h3>Discussion</h3><p>Guidance.</p>'
        )
        with self.assertRaisesRegex(ValueError, "Unsupported statement structure"):
            BUILD.parse_family("ac", html, "https://www.cyber.gc.ca/test")


class ExportTests(unittest.TestCase):
    def test_multiple_assigned_values_and_utf8_are_retained(self):
        params = {"p": {"id": "p", "values": ["première", "second"]}}
        self.assertEqual(EXPORT.render_text("{{ insert: param, p }}", params), "[CCCS Assignment: première; second]")

    def test_missing_and_cyclic_parameters_are_errors(self):
        with self.assertRaisesRegex(ValueError, "Unresolved parameter"):
            EXPORT.render_text("{{ insert: param, missing }}", {})
        params = {"p": {"id": "p", "values": ["{{ insert: param, q }}"]}, "q": {"id": "q", "label": "{{ insert: param, p }}"}}
        with self.assertRaisesRegex(ValueError, "Cyclic parameter"):
            EXPORT.render_text("{{ insert: param, p }}", params)

    def test_nested_groups_root_parameters_and_no_global_row_state(self):
        catalog = {"params": [{"id": "p", "values": ["assigned"]}], "groups": [{"groups": [{"controls": [sample_control()]}]}]}
        catalog["groups"][0]["groups"][0]["controls"][0]["parts"][0]["prose"] = "{{ insert: param, p }}"
        rows = EXPORT.catalog_to_rows(catalog)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(EXPORT.catalog_to_rows(catalog)), 1)
        self.assertIn("assigned", rows[0][3])
        self.assertIn("Canadian guidance.", rows[0][4])
        self.assertEqual(len(rows[0]), len(EXPORT.HEADERS))

    def test_withdrawn_parent_does_not_hide_active_children(self):
        parent = sample_control()
        parent["props"].append({"name": "status", "value": "withdrawn"})
        parent["controls"] = [sample_control("ac-1.1")]
        rows = EXPORT.catalog_to_rows({"controls": [parent]})
        self.assertEqual([row[1] for row in rows], ["AC-1.1"])

    def test_active_control_without_a_statement_is_an_error(self):
        control = sample_control()
        control["parts"] = [control["parts"][1]]
        with self.assertRaisesRegex(ValueError, "no statement"):
            EXPORT.catalog_to_rows({"controls": [control]})

    def test_duplicate_parameters_are_an_error(self):
        with self.assertRaisesRegex(ValueError, "Duplicate parameter ID"):
            EXPORT.catalog_to_rows({"params": [{"id": "p"}, {"id": "p"}]})

    def test_group_level_parts_are_not_exported_as_controls(self):
        group = {"id": "ac", "title": "Access control", "parts": [{"name": "overview", "prose": "Family description."}], "controls": [sample_control()]}
        self.assertEqual(len(EXPORT.catalog_to_rows({"groups": [group]})), 1)


class ResolutionTests(unittest.TestCase):
    def test_local_provenance_becomes_portable_but_external_links_are_unchanged(self):
        target = ROOT / "profiles" / "test" / "resolved.json"
        original = {"links": [{"href": (ROOT / "imports" / "catalog.json").as_uri() + "#part"}, {"href": "https://www.cyber.gc.ca/en/source"}]}
        RESOLVE.portable_links(original, target)
        self.assertEqual(original["links"][0]["href"], "../../imports/catalog.json#part")
        self.assertEqual(original["links"][1]["href"], "https://www.cyber.gc.ca/en/source")

    def test_resolution_metadata_and_uuid_are_deterministic(self):
        profile = {"uuid": "e057c6c7-c436-44b4-9d75-c503eef04f37", "metadata": {"last-modified": BUILD.MODIFIED}}
        first = {"uuid": "random-one", "metadata": {"last-modified": "2020-01-01T00:00:00Z"}}
        second = {"uuid": "random-two", "metadata": {"last-modified": "2026-01-01T00:00:00Z"}}
        RESOLVE.normalize(first, profile, ROOT / "resolved.json")
        RESOLVE.normalize(second, profile, ROOT / "resolved.json")
        self.assertEqual(first, second)

    def test_resolution_retains_copyright_and_authoritative_source_metadata(self):
        profile = {
            "uuid": "e057c6c7-c436-44b4-9d75-c503eef04f37",
            "metadata": {
                "last-modified": BUILD.MODIFIED, "remarks": BUILD.COPYRIGHT,
                "links": [{"href": BUILD.BASE + BUILD.GUIDANCE, "rel": "reference"}],
            },
        }
        catalog = {"metadata": {"links": [{"href": "", "rel": "source-profile"}]}}
        RESOLVE.normalize(catalog, profile, ROOT / "example-resolved.json")
        self.assertIn("Crown copyright", catalog["metadata"]["remarks"])
        self.assertIn({"href": "example-profile.json", "rel": "source-profile"}, catalog["metadata"]["links"])

    def test_java_json_field_order_does_not_change_serialized_artifacts(self):
        first = {"catalog": {"metadata": {"title": "Title", "version": "2"}, "controls": []}}
        second = {"catalog": {"controls": [], "metadata": {"version": "2", "title": "Title"}}}
        self.assertEqual(RESOLVE.serialize(first), RESOLVE.serialize(second))

    def test_failed_resolution_does_not_replace_preexisting_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / "one-profile.json", root / "two-profile.json"]
            for path in paths:
                path.write_text(json.dumps({"profile": {"uuid": "e057c6c7-c436-44b4-9d75-c503eef04f37", "metadata": {"last-modified": BUILD.MODIFIED}}}))
                path.with_name(path.name.replace("-profile", "-resolved")).write_text("previous output")

            def execute(command, **kwargs):
                if command[-1] == "--version":
                    return subprocess.CompletedProcess(command, 0, stdout="oscal-cli 3.2.0\n")
                if "resolve-profile" in command:
                    if Path(command[-2]) == paths[1]:
                        raise subprocess.CalledProcessError(1, command)
                    Path(command[-1]).write_text(json.dumps({"catalog": {"uuid": "random", "metadata": {}, "controls": [sample_control()]}}))
                return subprocess.CompletedProcess(command, 0)

            with patch.object(RESOLVE, "ROOT", root), patch.object(RESOLVE, "PROFILE_PATHS", paths), patch.object(RESOLVE.shutil, "which", return_value="fake"), patch.object(RESOLVE.subprocess, "run", side_effect=execute), patch.object(sys, "argv", ["resolve.py"]):
                with self.assertRaises(subprocess.CalledProcessError):
                    RESOLVE.main()
            for path in paths:
                self.assertEqual(path.with_name(path.name.replace("-profile", "-resolved")).read_text(), "previous output")


class ResolvedArtifactTests(unittest.TestCase):
    def test_all_resolved_catalogs_and_csv_exports_match(self):
        for profile_path in RESOLVE.PROFILE_PATHS:
            path = profile_path.with_name(profile_path.name.replace("-profile", "-resolved"))
            catalog = read(path)["catalog"]
            with self.subTest(profile=path.parent.name):
                self.assertEqual(catalog["metadata"]["oscal-version"], BUILD.OSCAL_VERSION)
                self.assertEqual(catalog["metadata"]["last-modified"], BUILD.MODIFIED)
                self.assertNotRegex(json.dumps(catalog), r'"href":\s*"file:')
                with path.with_suffix(".csv").open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.reader(stream))
                self.assertEqual(rows[0], EXPORT.HEADERS)
                self.assertEqual(rows[1:], EXPORT.catalog_to_rows(catalog))
                statement_parts = [
                    part
                    for control in BUILD.walk_controls(catalog) if active(control)
                    for statement in control["parts"] if statement["name"] == "statement"
                    for part in BUILD.statement_parts(statement)
                ]
                self.assertEqual(len(rows) - 1, len(statement_parts))
                self.assertEqual(
                    {row[1] for row in rows[1:]},
                    {c["id"].upper() for c in BUILD.walk_controls(catalog) if active(c)},
                )

    def test_assigned_values_survive_actual_profile_resolution(self):
        for name in ProfileTests.LEGACY_HASHES:
            path = ROOT / "profiles" / name / f"{name}.json"
            profile = read(path)["profile"]
            catalog = read(path.with_name(path.name.replace("-profile", "-resolved")))["catalog"]
            params = {p["id"]: p for c in EXPORT.walk(catalog) for p in c.get("params", [])}
            for assignment in profile.get("modify", {}).get("set-parameters", []):
                with self.subTest(profile=name, param=assignment["param-id"]):
                    self.assertEqual(params[assignment["param-id"]]["values"], assignment["values"])


if __name__ == "__main__":
    unittest.main()
