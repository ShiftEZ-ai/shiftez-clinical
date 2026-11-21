"""Check workflow triggers, action pins, hosted provenance and destination fields."""
import os
from pathlib import Path
import re
import sys

if policy_yaml := os.getenv("POLICY_YAML_PATH"):
    sys.path.insert(0, policy_yaml)
import yaml

HOSTED_STEP={"name":"Require GitHub-hosted execution","env":{"HOSTED_KIND":"${{ runner.environment }}"},
             "working-directory":".","run":'test "$HOSTED_KIND" = "github-hosted" || { echo "GitHub-hosted runner required"; exit 1; }'}
ALLOWED_TRIGGERS = {"push", "pull_request", "merge_group", "schedule", "workflow_dispatch", "workflow_call"}
LOCAL_ACTIONS = "./.github/actions/"
REUSABLE_WORKFLOW = "./.github/workflows/ci-tests.yml"

class WorkflowLoader(yaml.BaseLoader):
    pass

def mapping(loader,node):
    result={}
    for key,value in node.value:
        name=loader.construct_object(key,deep=True)
        if not isinstance(name,str) or name in result:raise ValueError("Duplicate/invalid workflow key")
        result[name]=loader.construct_object(value,deep=True)
    return result

WorkflowLoader.add_constructor('tag:yaml.org,2002:map',mapping)

def load(text):
    try:
        return yaml.load(text, Loader=WorkflowLoader)
    except yaml.YAMLError:
        raise ValueError("Invalid workflow YAML") from None

def local_action(reference):
    if not isinstance(reference, str) or not reference.startswith(LOCAL_ACTIONS):
        return False
    segments = reference.removeprefix(LOCAL_ACTIONS).removesuffix("/").split("/")
    return all(part not in {".", ".."} and re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in segments)

SHELLS = {"bash", "sh", "pwsh", "python"}


def check(text):
    if re.search(r"environment\.url|\b(?:environment_url|target_url)\b",text,re.I):
        raise ValueError("Environment/deployment/commit-status destination URLs are forbidden")
    document = load(text)
    if not isinstance(document, dict):
        raise ValueError("Workflow must be a mapping")
    for key in ("on", True, "true"):
        if key not in document:
            continue
        triggers = document[key]
        if isinstance(triggers, str):
            triggers = (triggers,)
        if not isinstance(triggers, (dict, list, tuple)):
            raise ValueError("Workflow triggers must use a name, list or mapping")
        for trigger in triggers:
            if not isinstance(trigger, str) or not trigger:
                raise ValueError("Workflow trigger must be a name")
            if trigger not in ALLOWED_TRIGGERS:
                raise ValueError(trigger + " is forbidden as a workflow trigger")
    jobs = document.get("jobs", {})
    if not isinstance(jobs, dict):
        raise ValueError("Workflow jobs must be a mapping")
    job_ids = {id(job) for job in jobs.values()}

    def walk(value,active):
        if not isinstance(value,(dict,list)):return
        if id(value) in active:raise ValueError("Recursive workflow YAML is forbidden")
        active=active|{id(value)}
        if isinstance(value,dict):
            for key,item in value.items():
                if isinstance(key, str) and key.casefold() in {"url","environment_url","target_url"}:
                    raise ValueError("Workflow destination URL fields are forbidden")
                if key == "shell" and item not in SHELLS:
                    raise ValueError("Workflow shell must be one of bash, sh, pwsh or python")
                if key=="runs-on" and item!="ubuntu-24.04":
                    raise ValueError("Only the reviewed GitHub-hosted ubuntu-24.04 label is allowed")
                if key == "uses":
                    local = local_action(item) or (item == REUSABLE_WORKFLOW and id(value) in job_ids)
                    pinned = isinstance(item, str) and re.fullmatch(
                        r"[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-fA-F]{40}", item, re.ASCII
                    )
                    if not (local or pinned):
                        raise ValueError("Workflow uses must be an approved local path or pinned to a full commit SHA")
                walk(item,active)
        else:
            for item in value:walk(item,active)
    walk(document,set())
    for name,job in jobs.items():
        if not isinstance(job,dict):raise ValueError('Workflow job must be a mapping')
        if 'uses' in job:
            if job['uses'] != REUSABLE_WORKFLOW:
                raise ValueError('External/unreviewed reusable workflow is forbidden: '+str(name))
            continue
        if job.get('runs-on')!='ubuntu-24.04':raise ValueError('Runnable job needs reviewed hosted label: '+str(name))
        steps=job.get('steps',[])
        if (not isinstance(steps,list) or not steps or not isinstance(steps[0],dict)
                or any(steps[0].get(k)!=v for k,v in HOSTED_STEP.items())
                or 'if' in steps[0] or 'continue-on-error' in steps[0]):
            raise ValueError('First step must assert runner.environment == github-hosted: '+str(name))
    return document

def main():
    folder=Path(sys.argv[1])
    for path in sorted([*folder.glob('*.yml'),*folder.glob('*.yaml')]):check(path.read_text(encoding='utf-8'))

if __name__=='__main__':main()
