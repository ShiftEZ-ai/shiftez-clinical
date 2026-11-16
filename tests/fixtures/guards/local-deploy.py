def deploy_fixture():
    if args.migrate and head != current:
        phase = "verified backup"
        receipt["backup"] = str(backup(e, database, **({"output": output} if output is not None else {})))
    if not args.app_only:
        phase = "worker credentials"
        provision_worker(e, database, worker_password)
    if args.migrate and head != current:
        phase, migration_attempted = "migration", True
        run("docker", "run", "--rm", "--network", e["network"], "--env-file", str(migrator), *runtime_mode, args.image,
            "python", "scripts/manage.py", "migrate", *confirm)
    if not args.app_only:
        phase = "database grants"
        run("docker", "exec", "-i", e["db_container"], "psql", "-U", "postgres", "-d", database, "-v", "ON_ERROR_STOP=1", "-q",
            input=grants_sql if grants_sql is not None else (ROOT / "infra/database-roles.sql").read_text())
    if (args.migrate and head != current) or args.resume_post_migration:
        phase = "runtime privacy function privilege"
        run(*runtime_privilege_check)
        # Derived rows only: failure does not undo the schema migration already committed above.
        phase = "fairness rebuild"
        print(run("docker", "run", "--rm", "--network", e["network"], "--env-file", str(migrator), *runtime_mode, args.image,
                  "python", "scripts/manage.py", "rebuild-fairness"), flush=True)
    for role in ("api", "worker"):
        pass
