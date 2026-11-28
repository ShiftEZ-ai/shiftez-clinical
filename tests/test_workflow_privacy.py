"""Pipeline regression: environments may disclose names, never destination URLs."""
import base64
import gzip
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import yaml

HELPERS = Path(__file__).resolve().parents[1] / ".github/scripts"
if not HELPERS.is_dir():
    HELPERS = Path(__file__).resolve().parent
sys.path.insert(0, str(HELPERS))
import workflow_privacy

SHELL = "C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash")


class WorkflowPrivacyTests(unittest.TestCase):
    def test_pr1_workflows_use_read_only_guard_events(self):
        root=HELPERS if (HELPERS/'ci.yml').is_file() else HELPERS.parent/'workflows'
        for name in ('ci.yml','ci-tests.yml','working-policy.yml'):
            text=(root/name).read_text()
            self.assertNotIn('pull_request_target',text)
        guard=yaml.load((root/'working-policy.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(guard['on']),{'pull_request','merge_group'})
        self.assertEqual(guard['permissions'],{'contents':'read'})
        checkout=next(step['with'] for step in guard['jobs']['policy']['steps'] if step.get('uses','').startswith('actions/checkout@'))
        self.assertEqual(checkout['ref'],'refs/heads/${{ github.event.repository.default_branch }}')
        self.assertEqual(checkout['persist-credentials'],'false')

    def test_all_workflows_match_privacy_policy(self):
        root = HELPERS if (HELPERS / "ci.yml").is_file() else HELPERS.parent / "workflows"
        for path in sorted([*root.glob("*.yml"), *root.glob("*.yaml")]):
            with self.subTest(workflow=path.name):
                workflow_privacy.check(path.read_text(encoding="utf-8"))

    def test_only_reviewed_trigger_names_are_accepted_in_each_form(self):
        names = ["push", "pull_request", "merge_group", "schedule", "workflow_dispatch", "workflow_call"]
        for name in names:
            with self.subTest(name=name):
                workflow_privacy.check(f"on: {name}")
        workflow_privacy.check(yaml.safe_dump({"on": names}))
        workflow_privacy.check(yaml.safe_dump({"on": dict.fromkeys(names)}))
        with patch.object(workflow_privacy, "load", return_value={True: dict.fromkeys(names)}):
            workflow_privacy.check("")

    def test_other_trigger_names_are_refused_in_each_form(self):
        for name in ("issues", "discussion_comment", "repository_dispatch", "release", "future_event"):
            for text in (f"on: {name}", f"on: [push, {name}]", f'"on": {{{name}: null}}'):
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, name + " is forbidden"):
                    workflow_privacy.check(text)
            with patch.object(workflow_privacy, "load", return_value={True: [name]}):
                with self.assertRaisesRegex(ValueError, name + " is forbidden"):
                    workflow_privacy.check("")

    def test_nested_trigger_list_entry_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Workflow trigger must be a name"):
            workflow_privacy.check("on: [push, [workflow_call]]")

    def test_pull_request_target_is_refused_in_each_trigger_form(self):
        for text in ("on: pull_request_target", "on: [push, pull_request_target]",
                     '"on": {pull_request_target: {types: [opened]}}'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "pull_request_target is forbidden"):
                workflow_privacy.check(text)
        workflow_privacy.check("on: [push, pull_request, merge_group]")

    def test_workflow_run_and_issue_comment_are_refused_in_each_trigger_form(self):
        for trigger in ("workflow_run", "issue_comment"):
            for text in (f"on: {trigger}", f"on: [push, {trigger}]", f'"on": {{{trigger}: null}}',
                         f"true: [{trigger}]"):
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, trigger + " is forbidden"):
                    workflow_privacy.check(text)
            with patch.object(workflow_privacy, "load", return_value={True: {trigger: None}}):
                with self.assertRaisesRegex(ValueError, trigger + " is forbidden"):
                    workflow_privacy.check("")

    def test_local_and_pinned_actions_are_allowed(self):
        for action in ("./.github/actions/check", "actions/checkout@" + "a" * 40, "actions/cache/restore@" + "b" * 40):
            with self.subTest(action=action):
                workflow_privacy.check(yaml.safe_dump({"jobs": {"checks": {
                    "runs-on": "ubuntu-24.04", "steps": [workflow_privacy.HOSTED_STEP, {"uses": action}]
                }}}))
        workflow_privacy.check("jobs: {test: {uses: ./.github/workflows/ci-tests.yml}}")

    def test_local_uses_cannot_escape_actions_or_call_workflows_as_steps(self):
        for action in ("./actions/check", "./.github/actions/../check", "./.github/actions//check",
                       "./.github/actions/", "./.github/workflows/ci-tests.yml"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "approved local path"):
                workflow_privacy.check(yaml.safe_dump({"jobs": {"checks": {
                    "runs-on": "ubuntu-24.04", "steps": [workflow_privacy.HOSTED_STEP, {"uses": action}]
                }}}))
        with self.assertRaisesRegex(ValueError, "approved local path"):
            workflow_privacy.check("jobs: {test: {uses: ./.github/workflows/unreviewed.yml}}")

    def test_unpinned_or_invalid_actions_are_refused(self):
        for action in ("actions/checkout@v4", "actions/cache/restore@main", "actions/checkout@" + "a" * 39,
                       "actions/checkout@" + "z" * 40, "docker://example", "./"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "full commit SHA"):
                workflow_privacy.check(yaml.safe_dump({"jobs": {"checks": {
                    "runs-on": "ubuntu-24.04", "steps": [workflow_privacy.HOSTED_STEP, {"uses": action}]
                }}}))
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            workflow_privacy.check("jobs: {test: {uses: owner/repo@main}}")

    def test_step_and_default_shells_are_named_shells_only(self):
        hosted = workflow_privacy.HOSTED_STEP
        for shell in ("bash", "sh", "pwsh", "python"):
            with self.subTest(shell=shell):
                workflow_privacy.check(yaml.safe_dump({"jobs": {"checks": {"runs-on": "ubuntu-24.04",
                    "steps": [hosted, {"run": "true", "shell": shell}]}}}))
        for shell in ("./tools/shell", "/usr/bin/bash", "bash {0}", "perl {0}", "${{ vars.SHELL }}"):
            for document in ({"jobs": {"checks": {"runs-on": "ubuntu-24.04", "steps": [hosted, {"run": "true", "shell": shell}]}}},
                             {"defaults": {"run": {"shell": shell}}, "jobs": {"checks": {"runs-on": "ubuntu-24.04", "steps": [hosted]}}}):
                with self.subTest(shell=shell), self.assertRaisesRegex(ValueError, "Workflow shell must be one of"):
                    workflow_privacy.check(yaml.safe_dump(document))

    def test_hosted_label_allowed(self):
        workflow_privacy.check(yaml.safe_dump({"jobs":{"checks":{"runs-on":"ubuntu-24.04","steps":[workflow_privacy.HOSTED_STEP]}}}))

    def test_quoted_or_inline_runner_keys_cannot_hide_nonhosted_label(self):
        workflow_privacy.check(yaml.safe_dump({'jobs':{'checks':{'runs-on':'ubuntu-24.04','steps':[workflow_privacy.HOSTED_STEP]}}}))
        for text in ('"runs-on": self-hosted',"'runs-on': [self-hosted, linux]",'{"runs-on":"self-hosted"}'):
            with self.subTest(text=text),self.assertRaises(ValueError):workflow_privacy.check(text)

    def test_self_hosted_or_unreviewed_runner_is_refused(self):
        for label in ('self-hosted','[self-hosted, linux]','${{ vars.RUNNER }}','{group: private}'):
            with self.subTest(label=label),self.assertRaises(ValueError):workflow_privacy.check("runs-on: "+label)
    def test_name_only_environment_allowed(self):
        workflow_privacy.check(yaml.safe_dump({"jobs":{"publish":{"runs-on":"ubuntu-24.04","environment":{"name":"production"},"steps":[workflow_privacy.HOSTED_STEP]}}}))

    def test_inline_and_block_urls_refused(self):
        for value in ("environment: {name: dev, url: ${{ inputs.address }}}", "environment:\n  name: dev\n  url: https://example.invalid",
                      'environment: {"name": dev, "url": https://example.invalid}', "env:\n  X: ${{ environment.url }}"):
            with self.subTest(value=value), self.assertRaises(ValueError): workflow_privacy.check(value)

    def test_status_url_payloads_refused(self):
        for value in ("gh api statuses -f target_url=https://example.invalid", "gh api deployments/statuses --input body.json # environment_url",
                      'body={"environment_url":"https://example.invalid"}'):
            with self.subTest(value=value), self.assertRaises(ValueError): workflow_privacy.check(value)


class DefaultBranchTests(unittest.TestCase):
    REPO = Path(__file__).resolve().parents[1]
    TRIGGER = ["    branches:", "    # shortcut: dev is removed after the default branch is renamed to main.",
               "    - dev", "    - main"]

    def test_no_workflow_or_trusted_script_hard_codes_the_integration_branch(self):
        paths = [*sorted((self.REPO / ".github/workflows").glob("*.y*ml")), *sorted((self.REPO / ".github/scripts").glob("*.py")),
                 *sorted((self.REPO / "ci").glob("*.py")), self.REPO / "scripts/affected_tests.py"]
        self.assertGreater(len(paths), 10)
        offenders, triggers = [], 0
        for path in paths:
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines):
                where = path.relative_to(self.REPO).as_posix() + ":" + str(number + 1)
                if "refs/heads/dev" in line or "branch=dev" in line or "queue/dev/" in line:
                    offenders.append(where)
                if line.strip() == "- dev":
                    triggers += 1
                    if lines[number - 2:number + 2] != self.TRIGGER:
                        offenders.append(where)
        self.assertEqual(offenders, [])
        self.assertEqual(triggers, 2)
        for name in ("ci.yml", "dependency-graph.yml"):
            doc = yaml.load((self.REPO / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(doc["on"]["push"]["branches"], ["dev", "main"])

    def test_default_branch_image_keeps_the_dev_tag_names(self):
        doc = yaml.load((self.REPO / ".github/workflows/image.yml").read_text(), Loader=yaml.BaseLoader)
        script = next(step["run"] for step in doc["jobs"]["publish"]["steps"] if step.get("id") == "push")
        block = script[script.index("case "):script.index("esac") + 4]
        commit = "0123456789ab" + "c" * 28
        for default in ("dev", "main", ""):
            other = "main" if default == "dev" else "dev"
            for ref_type, ref_name, expected in (
                    ("branch", default, "img:dev img:dev-0123456789ab" if default else None),
                    ("tag", "v1.2.3-rc.2", "img:v1.2.3-rc.2"), ("tag", "v1.2.3", "img:v1.2.3"),
                    ("branch", other, None), ("branch", "release/v1.2.3", None), ("branch", "feature", None)):
                with self.subTest(default=default, ref=ref_type + "/" + ref_name):
                    env = {**os.environ, "REF_TYPE": ref_type, "REF_NAME": ref_name, "COMMIT": commit,
                           "DEFAULT_BRANCH": default}
                    result = subprocess.run([SHELL, "-c", 'set -euo pipefail; image=img\n' + block + '\necho "${tags[@]}"'],
                                            env=env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.stdout.strip() if result.returncode == 0 else None, expected, result.stderr)

    def test_public_image_requires_main_and_a_passing_scan_before_push_and_attestation(self):
        doc = yaml.load((self.REPO / ".github/workflows/image.yml").read_text(), Loader=yaml.BaseLoader)
        jobs = doc["jobs"]
        self.assertEqual(set(doc["on"]), {"push"})
        self.assertEqual(doc["on"]["push"], {"branches": ["main"], "tags": ["v*"]})
        self.assertEqual(doc["permissions"], {"contents": "read"})
        self.assertEqual(jobs["publish"]["permissions"], {
            "contents": "read", "packages": "write", "id-token": "write", "attestations": "write",
        })
        for name, job in jobs.items():
            if name != "publish":
                permissions = job.get("permissions", {})
                self.assertTrue(permissions == "read-all" or
                                (isinstance(permissions, dict) and set(permissions.values()) <= {"read"}))

        scan, login, push, public_push, attest = jobs["publish"]["steps"][-5:]
        self.assertEqual(scan["id"], "scan")
        self.assertNotIn("if", scan)
        scripts = "\n".join(step.get("run", "") for step in jobs["publish"]["steps"])
        self.assertEqual(scripts.count("docker buildx build "), 1)
        self.assertEqual(scripts.count("scripts/scan_public_image.py "), 1)
        self.assertNotIn("continue-on-error", scan)
        self.assertTrue(scan["run"].startswith("set -euo pipefail\n"))
        self.assertIn("--provenance=false", scan["run"])
        self.assertIn('--output "type=docker,dest=$archive" --iidfile "$iid"', scan["run"])
        self.assertIn("--build-arg PUBLIC_IMAGE=1", scan["run"])
        self.assertNotIn("--push", scan["run"])
        self.assertNotIn("--tag", scan["run"])
        self.assertNotIn("--load", scan["run"])
        self.assertNotIn("docker image tag", scan["run"])
        scanner = 'python3 scripts/scan_public_image.py "$archive" "$base_config" "$image_id"'
        self.assertIn(scanner, scan["run"].splitlines())
        self.assertLess(scan["run"].index(scanner), scan["run"].index("docker image load"))
        passed = f"steps.{scan['id']}.outcome == 'success'"
        self.assertEqual(login["if"], passed)
        self.assertEqual(push["id"], "push")
        self.assertEqual(push["if"], passed)
        self.assertEqual(push["env"]["IMAGE_ID"], "${{ steps.scan.outputs.image-id }}")
        main_push = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
        self.assertEqual(public_push["if"], f"{passed} && {main_push}")
        self.assertEqual(public_push["env"], {
            "IMAGE": "${{ steps.scan.outputs.public-image }}", "TAG": "${{ steps.scan.outputs.public-tag }}",
            "IMAGE_ID": "${{ steps.scan.outputs.image-id }}",
        })
        self.assertIn('docker push "$IMAGE:$TAG"', public_push["run"])
        self.assertIn('docker buildx imagetools inspect --raw "$IMAGE@$digest"', public_push["run"])
        self.assertEqual(jobs["publish"]["outputs"], {
            "digest": "${{ steps.push.outputs.digest }}", "public-digest": "${{ steps.public_push.outputs.digest }}",
        })
        self.assertEqual(attest["if"], f"steps.{public_push['id']}.outcome == 'success'")
        self.assertRegex(attest["uses"], r"^actions/attest-build-provenance@[0-9a-f]{40}$")
        self.assertEqual(attest["with"], {
            "subject-name": "${{ steps.scan.outputs.public-image }}",
            "subject-digest": "${{ steps.public_push.outputs.digest }}",
            "push-to-registry": "true",
            "create-storage-record": "false",
        })

    def test_only_public_build_disables_install_time_bytecode(self):
        dockerfile = (self.REPO / "infra/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG PUBLIC_IMAGE=0", dockerfile.splitlines())
        install = dockerfile.split("RUN if [ -n \"$CI_PIP_INDEX_URL\" ]; then", 1)[1].split("\nCOPY ", 1)[0]
        stubs = 'python() { printf "%s\\n" "$@"; }; groupadd() { :; }; useradd() { :; };\n'
        for public in ("0", "1"):
            with self.subTest(public=public):
                env = {**os.environ, "PUBLIC_IMAGE": public, "CI_PIP_INDEX_URL": ""}
                result = subprocess.run([SHELL, "-ec", stubs + 'if [ -n "$CI_PIP_INDEX_URL" ]; then' + install],
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0)
                self.assertIn("--require-hashes", result.stdout.splitlines())
                self.assertEqual("--no-compile" in result.stdout.splitlines(), public == "1")


def test_public_publish_stops_on_scan_failure_or_config_mismatch(tmp_path):
    repo = DefaultBranchTests.REPO
    doc = yaml.load((repo / ".github/workflows/image.yml").read_text(), Loader=yaml.BaseLoader)
    steps = {step.get("id"): step for step in doc["jobs"]["publish"]["steps"]}
    published = "sha256:" + "a" * 64

    def run(step, stubs, **values):
        output = tmp_path / "github-output"
        output.write_text("")
        env = {**os.environ, "GITHUB_OUTPUT": output.as_posix(), "GITHUB_STEP_SUMMARY": "/dev/null", **values}
        result = subprocess.run([SHELL, "-c", stubs + steps[step]["run"]],
                                env=env, cwd=repo, capture_output=True, text=True, timeout=30)
        return result.returncode, output.read_text().splitlines()

    scan = """
docker() {
  case "$1/$2" in
    buildx/build) printf '%s' "$IMAGE_ID" > "$RUNNER_TEMP/image-id" ;;
    image/load) return 91 ;;
  esac
}
git() { :; }
python3() { return 44; }
"""
    runtime = {"RUNNER_TEMP": tmp_path.as_posix(), "COMMIT": "0" * 40, "GITHUB_REPOSITORY": "owner/repo",
               "GITHUB_SERVER_URL": "", "IMAGE_ID": "matching"}
    assert run("scan", scan, **runtime) == (44, [])

    trace = tmp_path / "registry"
    push = """
docker() {
  case "$1/$2" in
    image/tag) [[ "$3" == "$IMAGE_ID" ]] ;;
    buildx/imagetools) [[ "$3/$4" == "inspect/--raw" && "$5" == "$PUBLISHED_IMAGE@$PUBLISHED" ]] ;;
    push/*)
      printf '%s\n' "$2" >> "$TRACE"
      printf '%s: digest: %s\n' "${2##*:}" "$PUBLISHED"
      ;;
    *) return 90 ;;
  esac
}
jq() { printf '%s\n' "$REMOTE_CONFIG"; }
"""
    primary = "ghcr.io/owner/repo"
    public = primary + "-public"
    for step, ref_type, ref_name, image, tags in (
        ("push", "branch", "main", primary, ["dev", "dev-000000000000"]),
        ("push", "tag", "v1.2.3-rc.2", primary, ["v1.2.3-rc.2"]),
        ("push", "tag", "v1.2.3", primary, ["v1.2.3"]),
        ("public_push", "branch", "main", public, ["dev-000000000000"]),
    ):
        for remote in ("wrong", "matching"):
            trace.write_text("")
            result = run(step, push, **runtime, IMAGE=image, TAG=tags[0], REF_TYPE=ref_type, REF_NAME=ref_name,
                         DEFAULT_BRANCH="main", REMOTE_CONFIG=remote, PUBLISHED=published,
                         PUBLISHED_IMAGE=image, TRACE=trace.as_posix())
            expected = [f"digest={published}"] if remote == "matching" else []
            assert result == (int(remote != "matching"), expected)
            assert trace.read_text().splitlines() == [f"{image}:{tag}" for tag in tags]


def image_tar(files):
    data = BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, BytesIO(content))
    return data.getvalue()


def image_digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_image_build_step_requires_verified_layers_without_findings(tmp_path):
    repo = DefaultBranchTests.REPO
    doc = yaml.load((repo / ".github/workflows/image.yml").read_text(), Loader=yaml.BaseLoader)
    scan = next(step["run"] for step in doc["jobs"]["publish"]["steps"] if step.get("id") == "scan")
    stubs = """
git() { :; }
python3() { "$TEST_PYTHON" "$@"; }
docker() {
  case "$1/$2" in
    buildx/imagetools) cat "$FIXTURE_BASE" ;;
    buildx/create) : ;;
    buildx/build)
      while (( $# )); do
        case "$1" in
          --output) cp "$FIXTURE_ARCHIVE" "${2#type=docker,dest=}"; shift ;;
          --iidfile) printf '%s' "$FIXTURE_ID" > "$2"; shift ;;
          --tag|--push) return 90 ;;
        esac
        shift
      done
      ;;
    image/load) echo 'loaded' >> "$TRACE" ;;
    *) return 90 ;;
  esac
}
"""
    base = image_tar({"base.txt": b"clean"})
    # Construct synthetic values at runtime so the source tree stays clean.
    marker = ("gh" + "p_" + "aB3d" * 9).encode()
    address = ("10." + "20.30.40").encode()
    base_config = tmp_path / "base.json"
    fixture = tmp_path / "fixture.tar"
    output = tmp_path / "output"
    trace = tmp_path / "trace"

    def config(layers):
        return json.dumps({"os": "linux", "architecture": "amd64",
                           "rootfs": {"type": "layers", "diff_ids": [image_digest(layer) for layer in layers]}}).encode()

    base_config.write_bytes(config([base]))
    for expected in (0, 1, 2):
        first = image_tar({"removed.txt": marker + b"\n" + address if expected == 1 else b"clean"})
        later = image_tar({".wh.removed.txt": b""})
        content = config([base, first, later])
        manifest = [{"Config": "config.json", "Layers": ["base.tar", "first.tar", "later.tar"]}]
        fixture.write_bytes(image_tar({"manifest.json": json.dumps(manifest).encode(), "config.json": content,
                                       "base.tar": base, "first.tar": first, "later.tar": later}))
        output.write_bytes(b"")
        trace.write_bytes(b"")
        image_id = image_digest(b"wrong config" if expected == 2 else content)
        env = {**os.environ, "GITHUB_OUTPUT": output.as_posix(), "RUNNER_TEMP": tmp_path.as_posix(),
               "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SERVER_URL": "", "COMMIT": "0" * 40,
               "TEST_PYTHON": Path(sys.executable).as_posix(), "FIXTURE_BASE": base_config.as_posix(),
               "FIXTURE_ARCHIVE": fixture.as_posix(), "FIXTURE_ID": image_id, "TRACE": trace.as_posix()}
        result = subprocess.run([SHELL, "-c", stubs + scan], env=env, cwd=repo, capture_output=True, timeout=30)
        assert result.returncode == expected
        assert all(value not in result.stdout + result.stderr for value in (marker, address))
        if expected:
            assert output.read_bytes() == trace.read_bytes() == b""
            if expected == 1:
                assert result.stdout.splitlines()[:3] == [b"github-token: 1", b"ipv4: 1", b"Image privacy scan: 2 hits"]
            else:
                assert b"Image privacy scan could not verify or read its inputs." in result.stderr
        else:
            assert b"Image privacy scan: 0 hits" in result.stdout
            assert output.read_text().splitlines() == [
                f"image-id={image_id}", "public-image=ghcr.io/owner/repo-public", "public-tag=dev-000000000000",
            ]
            assert trace.read_text().splitlines() == ["loaded"]


def test_public_image_scan_verifies_upstream_content_and_scans_the_rest(tmp_path, capsys):
    from scripts import scan_public_image

    marker = "planted value"

    image = tmp_path / "image.tar"
    base_config = tmp_path / "base.json"
    exceptions = tmp_path / "exceptions.json"
    upstream = marker.encode()
    dpkg_status = "var/lib/dpkg/status"
    dpkg_line = b"Depends: " + upstream + b"\xff"
    base_status = b"Version: 1\n" + dpkg_line + b"\n"
    base_layer = image_tar({"base.txt": upstream, dpkg_status: base_status})
    upgraded_status = b"Version: 2\n" + dpkg_line + b"\n\nPackage: extra\nVersion: 1\n"
    root = "usr/local/lib/python3.13/site-packages"
    installed = f"{root}/module.py"
    record = f"{root}/module.dist-info/RECORD"
    wheel_digest = base64.urlsafe_b64encode(hashlib.sha256(upstream).digest()).decode().rstrip("=")
    wheel_record = f"module.py,sha256={wheel_digest},{len(upstream)}\n".encode()
    excepted = "generated.bin"
    exceptions.write_text(json.dumps({excepted: {hashlib.sha256(upstream).hexdigest(): ["test-pattern"]}}))

    def config(layers, value="clean"):
        return {"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": layers},
                "config": {"Env": [f"VALUE={value}"]}}

    def make_image(files, later, *, value="clean", changed_base=False, missing_config=False):
        layers = [base_layer, image_tar(files), image_tar(later)]
        content = json.dumps(config([image_digest(layer) for layer in layers], value)).encode()
        manifest = [{"Config": "config.json", "Layers": ["base.tar.gz", "first.tar", "second.tar"]}]
        members = {
            "manifest.json": json.dumps(manifest).encode(),
            "base.tar.gz": gzip.compress(image_tar({"new-base.txt": upstream}) if changed_base else base_layer),
            "first.tar": layers[1],
            "second.tar": layers[2],
        }
        if not missing_config:
            members["config.json"] = content
        image.write_bytes(image_tar(members))
        return image_digest(content)

    def findings(text):
        return [(number, "test-pattern") for number, line in enumerate(text.split("\n"), 1) if marker in line]

    base_config.write_text(json.dumps(config([image_digest(base_layer)])))
    good = {installed: upstream, record: wheel_record, excepted: upstream, dpkg_status: upgraded_status}
    removed = {".wh.removed.txt": b""}
    with (patch.dict(sys.modules, {"privacy_check": SimpleNamespace(findings=findings)}),
          patch.object(scan_public_image, "EXCEPTIONS", exceptions)):
        for files, later, value, expected in (
            (good, removed, "clean", 0),
            (good, removed, marker, 1),
            ({**good, "removed.txt": upstream}, removed, "clean", 1),
            ({**good, installed: upstream + b" changed"}, removed, "clean", 1),
            ({installed: upstream}, removed, "clean", 1),
            (good, {installed: upstream}, "clean", 1),
            ({**good, excepted: upstream + b" changed"}, removed, "clean", 1),
            ({**good, "new.bin": upstream}, removed, "clean", 1),
            ({**good, dpkg_status: upgraded_status + upstream + b"\n"}, removed, "clean", 1),
            ({**good, dpkg_status: upgraded_status.replace(b"\xff", b"\xfe")}, removed, "clean", 1),
            ({**good, "var/lib/dpkg/status-old": dpkg_line}, removed, "clean", 1),
        ):
            image_id = make_image(files, later, value=value)
            assert scan_public_image.main(image, base_config, image_id) == expected
            result = capsys.readouterr()
            assert marker not in result.out + result.err
            assert f"Image privacy scan: {expected} hits" in result.out
            if not expected:
                assert f"skipped_files=3 skipped_bytes={2 * len(upstream) + len(base_status)} excepted_hits=2" in result.out
        for changes in ({"changed_base": True}, {"missing_config": True}):
            image_id = make_image(good, removed, **changes)
            assert scan_public_image.main(image, base_config, image_id) == 2
        image_id = make_image(good, removed)
        assert scan_public_image.main(image, base_config, image_digest(b"wrong cfg")) == 2
        base_config.write_text(json.dumps(config([image_digest(b"wrong base")])))
        assert scan_public_image.main(image, base_config, image_id) == 2


if __name__ == "__main__":
    unittest.main()
