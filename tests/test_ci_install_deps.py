from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INSTALL = "timeout --kill-after=30 600 python -m playwright install-deps chromium"
STOP = "sudo pkill -KILL -x apt-get || true; sudo pkill -KILL -x dpkg || true"
REPAIR = "sudo dpkg --configure -a || true"


def install_script():
    jobs = yaml.load((ROOT / ".github/workflows/ci-tests.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]
    steps = [step for job in jobs.values() for step in job["steps"] if "install-deps" in step.get("run", "")]
    assert len(steps) == 1 and "timeout-minutes" not in steps[0]
    return steps[0]["run"]


def checks_steps():
    jobs = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]
    return {step["name"]: step for step in jobs["checks"]["steps"] if "name" in step}


def test_checks_prepares_one_image_bound_apt_bundle():
    steps = checks_steps()
    download = steps["Download browser system packages"]
    archive = steps["Archive the shared downloads"]
    script = download["run"]
    assert download["if"] == archive["if"] == "steps.affected-scope.outputs.scope == ''"
    assert "apt_retry install --download-only -y --no-install-recommends" in script
    assert "mirror+file:" in script and script.count("https://") >= 3
    assert "arch=amd64" in script and 'Acquire::Languages "none"' in script
    assert 'Dir::Etc::sourcelist "$apt_work/ubuntu.list"' in script and 'Dir::Etc::sourceparts "-"' in script
    # 3 x 120 s fits the Checks job's 10-min limit; a failed fetch leaves the shards their logged fallback.
    assert 'timeout --kill-after=15 120 sudo apt-get -c "$APT_CONFIG"' in script
    assert "for attempt in 1 2 3; do" in script
    assert '"${ImageVersion:' in script and '> "$apt_bundle/image-version"' in script
    assert 'ci-browser.tar" public-ci-playwright public-ci-apt' in archive["run"]


def run_script(tmp_path, stand_ins, script):
    path = tmp_path / "step.sh"
    path.write_text(stand_ins + "\n" + script, encoding="utf-8", newline="\n")
    run = subprocess.run(["bash", "-e", "-o", "pipefail", "./step.sh"], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    return run.stdout.splitlines()


def run_checks_download(tmp_path, plan, status, failing=""):
    (tmp_path / "playwright-plan").write_text(plan, encoding="utf-8", newline="\n")
    stand_ins = f"""
    RUNNER_TEMP=$PWD
    ImageVersion=matching
    python() {{
      test "$*" = '-m playwright install-deps --dry-run chromium' || return 2
      test -f "$APT_CONFIG" && test -f "$RUNNER_TEMP/indexes" || return 2
      cat "$RUNNER_TEMP/playwright-plan"
      return {status}
    }}
    timeout() {{
      test "$1 $2 $3 $4 $5" = '--kill-after=15 120 sudo apt-get -c' && test "$6" = "$APT_CONFIG" || return 2
      shift 6
      test "$1" = '{failing}' && return 1
      case "$1" in
        update) touch "$RUNNER_TEMP/indexes"; echo 'apt update';;
        install) test "$2 $3 $4 $5" = '--download-only -y --no-install-recommends -o' || return 2
                 archives=${{6#Dir::Cache::archives=}}
                 shift 6
                 printf 'download %s\\n' "$@"
                 for package in "$@"; do touch "$archives/${{package%%:*}}.deb"; done;;
        *) return 2;;
      esac
    }}
    """
    output = run_script(tmp_path, stand_ins, checks_steps()["Download browser system packages"]["run"])
    apt_bundle = tmp_path / "public-ci-apt"
    assert (apt_bundle / "image-version").read_text(encoding="utf-8") == "matching\n"
    return output, sorted(deb.name for deb in apt_bundle.glob("*.deb"))


def test_checks_downloads_each_package_in_the_playwright_plan(tmp_path):
    output, debs = run_checks_download(tmp_path, "Missing system dependencies (2):\n  browser-fonts\n  browser-runtime:amd64\n", 1)
    assert output == ["apt update", "download browser-fonts", "download browser-runtime:amd64"]
    assert debs == ["browser-fonts.deb", "browser-runtime.deb"]
    mirror_list, = tmp_path.glob("ci-apt.*/mirrors.list")
    rows = mirror_list.read_text(encoding="utf-8").splitlines()
    assert [row.partition("\t")[2] for row in rows] == ["priority:1", "priority:2", "priority:3"]


def test_checks_keeps_only_the_image_when_every_package_is_installed(tmp_path):
    assert run_checks_download(tmp_path, "All system dependencies are installed.\n", 0) == (["apt update"], [])


@pytest.mark.parametrize("plan, status", [
    ("Missing system dependencies (1):\n  --invalid\n", 1),
    ("Missing system dependencies (1):\n  browser-fonts\n", 2),
])
def test_checks_refuses_a_malformed_playwright_plan(tmp_path, plan, status):
    output, debs = run_checks_download(tmp_path, plan, status)
    assert output == ["apt update", "::warning::Playwright system package plan was invalid; browser shards will use fallback"]
    assert debs == []


def test_checks_refuses_a_playwright_plan_with_a_count_mismatch(tmp_path):
    output, debs = run_checks_download(tmp_path, "Missing system dependencies (2):\n  browser-fonts\n", 1)
    assert output == ["apt update", "::warning::Playwright system package plan was invalid; browser shards will use fallback"]
    assert debs == []


@pytest.mark.parametrize("failing, message", [
    ("update", "::warning::Browser APT index refresh failed; browser shards will use fallback"),
    ("install", "::warning::Browser APT package download failed; browser shards will use fallback"),
])
def test_a_failed_fetch_falls_back_without_failing_checks(tmp_path, failing, message):
    output, debs = run_checks_download(tmp_path, "Missing system dependencies (1):\n  browser-fonts\n", 1, failing)
    assert output[-1] == message and debs == []


def test_browser_system_dependencies_are_bounded_and_retried():
    script = install_script()
    assert INSTALL + " && break" in script
    assert "for attempt in 1 2 3; do" in script
    # A stalled attempt's apt-get child survives the timeout and holds the apt lock; it is stopped before the repair.
    assert 'if [ "$attempt" != 1 ]; then ' + STOP + "; " + REPAIR + "; fi" in script
    assert 'if [ "$attempt" = 3 ]; then' in script and "exit 1" in script
    assert "Acquire::Retries" in script and "Acquire::http::Timeout" in script


def run_browser_install(tmp_path, *, image="matching", with_debs=True, dpkg_status=0):
    apt_bundle = tmp_path / "public-ci-apt"
    apt_bundle.mkdir()
    (apt_bundle / "image-version").write_text("matching\n", encoding="utf-8", newline="\n")
    if with_debs:
        (apt_bundle / "browser.deb").touch()
    stand_ins = f"""
    RUNNER_TEMP=$PWD
    ImageVersion={image}
    tar() {{ :; }}
    sudo() {{
      case "$1 $2" in
        'apt-get install') test "${{*:3}}" = "-y --no-download --no-remove --no-install-recommends -o Dir::Cache::Archives=$RUNNER_TEMP/public-ci-apt/ $RUNNER_TEMP/public-ci-apt/browser.deb" \
                             && test "$#" = 9 || return 2
                           echo 'apt-get install'; return {dpkg_status};;
        'tee /etc/apt/apt.conf.d/80-ci-retries') cat;;
        *) return 2;;
      esac
    }}
    timeout() {{ echo 'online install'; }}
    """
    return run_script(tmp_path, stand_ins, install_script())


def test_browser_uses_offline_packages_for_a_matching_image(tmp_path):
    assert run_browser_install(tmp_path) == [
        "apt-get install", "::notice::browser system packages: offline (1 debs, image matching)",
    ]


def test_browser_falls_back_for_a_different_image(tmp_path):
    assert run_browser_install(tmp_path, image="different") == [
        "::warning::browser system packages: fallback (image version mismatch)", "online install",
    ]


def test_browser_falls_back_when_no_packages_were_bundled(tmp_path):
    assert run_browser_install(tmp_path, with_debs=False) == [
        "::warning::browser system packages: fallback (empty set)", "online install",
    ]


def test_browser_falls_back_when_the_offline_install_fails(tmp_path):
    assert run_browser_install(tmp_path, dpkg_status=1) == [
        "apt-get install", "::warning::browser system packages: fallback (offline install failed)", "online install",
    ]


def test_the_offline_set_is_planned_by_apt_not_raw_dpkg():
    # Issue 1842: raw dpkg unpacked a bundled runtime package upgrade after refusing its paired data package, and the
    # half-configured pair broke the online fallback too. APT orders the pair or refuses the set untouched.
    script = install_script()
    offline = script[:script.index("for attempt in 1 2 3; do")]
    assert "dpkg -i" not in offline and "--auto-deconfigure" not in offline
    # The bundle is APT's archive directory for this call, or --no-download would drop every named .deb.
    assert ('sudo apt-get install -y --no-download --no-remove --no-install-recommends \\\n'
            '    -o "Dir::Cache::Archives=$apt_bundle/" "${debs[@]}"') in offline


@pytest.mark.parametrize("behaviour, repairs, code", [
    ("exit 0", 0, 0),
    ("[ -f done ] && exit 0; touch done; exit 1", 1, 0),
    ("exit 1", 2, 1),
    ("exec sleep 5", 2, 1),  # a stalled install is killed and still retried after a dpkg repair
])
def test_the_install_loop_retries_and_repairs(tmp_path, behaviour, repairs, code):
    bash, timeout = shutil.which("bash"), shutil.which("timeout")
    assert bash and timeout, "bash and coreutils timeout are on every hosted runner"
    loop = install_script()
    loop = loop[loop.index("for attempt in 1 2 3; do"):]
    stand_in = (loop.replace(INSTALL, f"timeout --kill-after=1 1 bash -c '{behaviour}'")
                .replace(STOP, "echo stop-leftovers").replace(REPAIR, "echo dpkg-repair"))
    run = subprocess.run([bash, "-c", stand_in], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert run.returncode == code, run.stdout + run.stderr
    assert run.stdout.count("dpkg-repair") == repairs, run.stdout
    assert run.stdout.count("stop-leftovers\ndpkg-repair") == repairs, run.stdout
    if code:
        assert "did not install in 3 attempts" in run.stdout


def test_the_shard_evidence_upload_is_retried_once():
    steps = yaml.load((ROOT / ".github/workflows/ci-tests.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]["tests"]["steps"]
    names = [step.get("name") for step in steps]
    first = steps[names.index("Retain first-attempt reports and failure evidence")]
    retry = steps[names.index("Retry the evidence upload once")]
    assert names.index("Retry the evidence upload once") == names.index("Retain first-attempt reports and failure evidence") + 1
    assert first["id"] == "evidence" and first["continue-on-error"] == "true" and first["if"] == "always()"
    assert retry["if"] == "always() && steps.evidence.outcome == 'failure'" and "continue-on-error" not in retry
    assert retry["uses"] == first["uses"]
    assert {**first["with"], "overwrite": "true"} == retry["with"]

    paths = set(first["with"]["path"].splitlines())
    assert {"candidate/.run/reports/", "candidate/.run/traces/", "candidate/.run/api-logs/"} <= paths
    assert "${{ inputs.suite == 'browser' && 'candidate/.run/captures/*.png' || '' }}" in paths


def test_pull_request_checks_run_the_pipeline_tests_as_a_merge_group():
    jobs = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["jobs"]
    step = next(step for step in jobs["static"]["steps"] if step.get("name") == "Pipeline tests under the merge-group event")
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "GITHUB_EVENT_NAME=merge_group python -m pytest" in step["run"]
    pattern = step["run"].split("grep -lE '", 1)[1].split("'", 1)[0]
    for module in ("make_known_failures", "trusted_ci", "trusted_verdict", "canary_evidence", "latest_reports", "working_policy"):
        assert module in pattern.split("|")
