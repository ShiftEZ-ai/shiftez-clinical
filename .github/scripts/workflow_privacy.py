"""Parse workflow policy: hosted provenance, local reusable workflows and no destination URLs."""
from pathlib import Path
import re
import sys
import yaml

HOSTED_STEP={"name":"Require GitHub-hosted execution","env":{"HOSTED_KIND":"${{ runner.environment }}"},
             "working-directory":".","run":'test "$HOSTED_KIND" = "github-hosted" || { echo "GitHub-hosted runner required"; exit 1; }'}

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

def check(text):
    if re.search(r"environment\.url|\b(?:environment_url|target_url)\b",text,re.I):
        raise ValueError("Environment/deployment/commit-status destination URLs are forbidden")
    try:document=yaml.load(text,Loader=WorkflowLoader)
    except yaml.YAMLError as error:raise ValueError("Invalid workflow YAML") from None
    def walk(value,active):
        if not isinstance(value,(dict,list)):return
        if id(value) in active:raise ValueError("Recursive workflow YAML is forbidden")
        active=active|{id(value)}
        if isinstance(value,dict):
            for key,item in value.items():
                if key.casefold() in {"url","environment_url","target_url"}:
                    raise ValueError("Workflow destination URL fields are forbidden")
                if key=="runs-on" and item!="ubuntu-24.04":
                    raise ValueError("Only the reviewed GitHub-hosted ubuntu-24.04 label is allowed")
                walk(item,active)
        else:
            for item in value:walk(item,active)
    walk(document,set())
    if not isinstance(document,dict):return
    jobs=document.get('jobs',{})
    if not isinstance(jobs,dict):raise ValueError('Workflow jobs must be a mapping')
    for name,job in jobs.items():
        if not isinstance(job,dict):raise ValueError('Workflow job must be a mapping')
        if 'uses' in job:
            if job['uses']!='./.github/workflows/ci-tests.yml':
                raise ValueError('External/unreviewed reusable workflow is forbidden: '+str(name))
            continue
        if job.get('runs-on')!='ubuntu-24.04':raise ValueError('Runnable job needs reviewed hosted label: '+str(name))
        steps=job.get('steps',[])
        if (not isinstance(steps,list) or not steps or not isinstance(steps[0],dict)
                or any(steps[0].get(k)!=v for k,v in HOSTED_STEP.items())
                or 'if' in steps[0] or 'continue-on-error' in steps[0]):
            raise ValueError('First step must assert runner.environment == github-hosted: '+str(name))

def main():
    folder=Path(sys.argv[1])
    for path in sorted([*folder.glob('*.yml'),*folder.glob('*.yaml')]):check(path.read_text(encoding='utf-8'))

if __name__=='__main__':main()
