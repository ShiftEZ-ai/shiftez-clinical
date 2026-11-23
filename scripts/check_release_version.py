"""Check that a release tag matches the app version stored in its target commit."""
import argparse
import ast
import re
import subprocess


NUMBER = r"(?:0|[1-9][0-9]*)"
RELEASE_TAG = re.compile(rf"v(?P<version>{NUMBER}\.{NUMBER}\.{NUMBER})(?:-rc\.{NUMBER})?")


def git(*args):
    return subprocess.check_output(["git", "--no-replace-objects", *args], stderr=subprocess.PIPE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("commit")
    args = parser.parse_args()
    tag = RELEASE_TAG.fullmatch(args.tag)
    if tag is None:
        parser.error("expected a tag of the form vX.Y.Z or vX.Y.Z-rc.N")

    try:
        commit = git("rev-parse", "--verify", "--end-of-options", args.commit + "^{commit}").decode().strip()
        source = ast.parse(git("show", f"{commit}:apps/api/version.py"))
        versions = []
        for statement in source.body:
            match statement:
                case ast.Assign(targets=[ast.Name(id="__version__")], value=ast.Constant(value=str() as value)):
                    versions.append(value)
        [version] = versions
    except (OSError, subprocess.CalledProcessError, SyntaxError, ValueError):
        parser.error("could not read a single literal app version from the target commit")

    expected = tag["version"]
    if version != expected:
        parser.error(f"tag {args.tag} requires version {expected}, but commit {commit} reports {version!r}")


if __name__ == "__main__":
    main()
