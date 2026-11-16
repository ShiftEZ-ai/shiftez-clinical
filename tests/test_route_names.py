"""Keep the shared route vocabulary in step with the web router and feedback redaction."""
import json
from pathlib import Path
import re

from apps.api.services import feedback
from packages.contracts.schemas import APP_ROUTE_PARTS


SRC = Path(__file__).resolve().parents[1] / "apps/web/src"


def _table(source, name):
    return source.split(f"const {name}:", 1)[1].split("={", 1)[1].split("\n};", 1)[0]


def test_web_route_names_match_contract():
    app = (SRC / "app.ts").read_text(encoding="utf-8")
    routes = (SRC / "routes.ts").read_text(encoding="utf-8")
    pages = set(re.findall(r"(?:^|,)\s*(\w+):\s*\{title:", _table(app, "paths"), re.M))
    tabs = _table(routes, "PAGE_TABS")
    tab_pages = set(re.findall(r"^\s*(\w+):\s*\[", tabs, re.M))
    tab_names = set(re.findall(r"\bkey:\s*['\"]([^'\"]+)['\"]", tabs))
    aliases = _table(routes, "ROUTE_ALIASES")
    # String aliases may share a line; function aliases carry record/query data that are not route names.
    alias_names = set(re.findall(r"^\s*(\w+):['\"]", aliases.replace(",", ",\n"), re.M))
    alias_names.update(re.findall(r"^\s*(\w+):\(", aliases, re.M))
    # The Schedule's literal subroutes (e.g. Resolve issues) are outside PAGE_TABS.
    subroutes = set(re.findall(r"routeParts\[0\]\s*[!=]==\s*['\"]([^'\"]+)['\"]", app))
    subroutes.update(re.findall(r"routeParts\.indexOf\(['\"]([^'\"]+)['\"]\)", app))
    assert pages and tab_pages and tab_names and alias_names and subroutes
    assert tab_pages <= pages
    web_names = pages | tab_names | alias_names | subroutes
    contract = set(APP_ROUTE_PARTS)
    assert web_names == contract, f"Missing from contract: {sorted(web_names - contract)}; stale: {sorted(contract - web_names)}"


def test_feedback_route_names_match_contract():
    assert len(APP_ROUTE_PARTS) == len(set(APP_ROUTE_PARTS))
    assert feedback._ROUTE_PARTS == frozenset(APP_ROUTE_PARTS)
    for part in APP_ROUTE_PARTS:
        assert feedback._safe_route(f"#{part}/synthetic-record?secret=private") == f"/{part}/[record]"


def test_generated_route_names_match_contract():
    generated = (SRC / "generated.ts").read_text(encoding="utf-8")
    names = re.search(r"^export const APP_ROUTE_PARTS = (.+) as const;$", generated, re.M)
    assert names, "Regenerate the route vocabulary with scripts/generate_contracts.py"
    assert json.loads(names[1]) == list(APP_ROUTE_PARTS)
