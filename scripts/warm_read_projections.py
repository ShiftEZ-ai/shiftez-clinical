"""Prewarm the deployed build before opening ingress; each workspace commits independently."""
from apps.api.db import make_engine
from apps.api.settings import Settings
from apps.api.services.read_projection_warming import warm_all


def main():
    settings = Settings.from_env()
    settings.check(require_authentication=False)
    engine = make_engine(settings.database_url, settings=settings)
    try:
        count = warm_all(engine, settings=settings)
        print(f'Prepared read projections for {count} workspaces (unchanged inputs reused)')
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
