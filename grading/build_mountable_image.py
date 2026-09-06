r"""Build the mountable grading image and print its id.

No Function references `mountable_image`, so `modal deploy` does not build it
and it has no id until this runs. `Sandbox.mount_image` needs one.

The grading deploy runs this after `modal deploy` and posts the id to Studio.
To refresh the image, re-run the deploy: `Deploy All [Dev]` or `Deploy All
[Prod]` in Actions, both take a `workflow_dispatch`. Building without deploying
puts the mounted grading code ahead of the lane's, which is the divergence this
path exists to avoid.

That leaves local iteration as the reason to run this directly, from this
directory:

    uv run --with modal python build_mountable_image.py \
        --modal-environment rl-studio-dev

An id resolves only in the Modal environment that built it, so dev and prod each
need their own run.

Environment:
    MODAL_TOKEN_ID      Modal API token ID (required)
    MODAL_TOKEN_SECRET  Modal API token secret (required)
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from modal_codeartifact import modal_environment_name, studio_modal_environment


async def main() -> int:
    ap = argparse.ArgumentParser()
    _ = ap.add_argument(
        "--modal-environment", default="rl-studio-dev", help="Modal environment"
    )
    _ = ap.add_argument(
        "--app",
        default="rl-studio-grading-mountable",
        help="build context only; the image outlives it",
    )
    _ = ap.add_argument(
        "--json-out",
        help="write one JSON object to this path, for a workflow to read",
    )
    args = ap.parse_args()

    # `dev` and `rl-studio-dev` both reach here, and Modal only knows the long
    # form. Normalised once so the variable, the lookup and the label agree.
    environment = studio_modal_environment(args.modal_environment)
    if environment is None:
        print(f"unknown environment {args.modal_environment}", file=sys.stderr)
        return 1

    # Steers the Modal client. modal_codeartifact reads MODAL_ENV ahead of this
    # one, so an inherited MODAL_ENV still wins the secret pick; the check below
    # is what catches that.
    os.environ["MODAL_ENVIRONMENT"] = environment

    # Below the assignment, not at the top of the file: importing modal_labs
    # runs the image definition, which reads the environment as it goes.
    import modal

    from modal_labs import mountable_image

    resolved = modal_environment_name()
    if resolved != environment:
        print(
            f"refusing to build: asked for {environment}, but the image "
            f"resolved its secret from {resolved}",
            file=sys.stderr,
        )
        return 1

    app = await modal.App.lookup.aio(
        args.app, create_if_missing=True, environment_name=environment
    )
    # A cold build runs apt, LibreOffice, a Node tarball and patchelf. Without
    # this it prints nothing until it finishes or fails.
    with modal.enable_output():
        built = await mountable_image.build.aio(app)

    # To a file, not stdout: enable_output above writes Modal's build logs to
    # stdout and stderr, so command substitution would capture those too.
    if args.json_out:
        _ = Path(args.json_out).write_text(
            json.dumps({"image_id": built.object_id, "modal_environment": environment})
        )
    print(f"\nimage_id: {built.object_id}")
    print(f"environment: {environment}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
