"""Single source of truth for the application version (SemVer). Release tags are v<this>."""
import os

__version__ = "0.8.0-rc.16"


def build():
    """What this process runs: one image holds the API, the worker and the web client. The image build sets the commit
    and date (infra/Dockerfile build args); the deploy names the image (ROTA_IMAGE_DIGEST). Unset outside an image."""
    commit = os.getenv("ROTA_BUILD_COMMIT", "").strip()[:7] or None
    image = os.getenv("ROTA_IMAGE_DIGEST", "").strip().rsplit("@", 1)[-1]
    return {"version": __version__, "commit": commit, "build_date": os.getenv("ROTA_BUILD_DATE", "").strip() or None,
            "image_digest": image[:19] if image.startswith("sha256:") else None,
            "dev_build": (os.getenv("ROTA_DEV_BUILD", "").strip() or None) if os.getenv("ROTA_DEPLOYMENT") == "dev" else None,
            # SemVer build metadata: two builds of one version (dev commits) still differ.
            "release": __version__ + (f"+{commit}" if commit else "")}
