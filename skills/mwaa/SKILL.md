---
name: mwaa
description: >-
  Where Amazon MWAA puts the files you upload to its S3 bucket (dags/, plugins.zip, requirements.txt, startup.sh) inside the Airflow containers under /usr/local/airflow, when each copy happens, how Python imports them, and how MWAA runs pip and the startup script. Use when deploying DAGs, plugins, requirements or a startup script to MWAA through the console, the S3 console, the AWS CLI or CI; when an uploaded plugins.zip, requirements.txt or startup.sh has no effect; when writing a custom constraints file or offline wheels; on "ModuleNotFoundError" or "Broken DAG: No module named" after a deploy; on "Could not open constraint file", "Forcing local constraints", "ResolutionImpossible" or "pip installation exited with a non-zero error code" in the requirements log; on "Failed to locate environment variables file from startup script"; when a startup.sh export does not reach tasks or pip; or when a container's filesystem needs inspecting without SSH.
---

# Amazon MWAA container filesystem

Verified against the MWAA image source, [amazon-mwaa-docker-images](https://github.com/aws/amazon-mwaa-docker-images/tree/32b2a2e81c99fa8b937dcb2eead71c9b9252127f) commit `32b2a2e` (`images/airflow/3.0.6`, same mechanics in `2.10.3` to `3.3.1`), Apache Airflow 3.0.6, pip 26.2.1 and the Amazon MWAA User Guide (2026-10-15), and by experiment with bash 5.2.21 and uv 0.13.1. Line-level source links are in `references/`.

## The map

Every S3 path you configure is relative to the environment's one source bucket, and each upload lands at a fixed container path whatever its S3 name:

```text
s3://bucket/<DagS3Path>/            -> /usr/local/airflow/dags/                         every 30 s, adds/changes/deletes
s3://bucket/<PluginsS3Path>         -> /usr/local/airflow/plugins/ (zip extracted)      on container start only
s3://bucket/<RequirementsS3Path>    -> /usr/local/airflow/requirements/requirements.txt on container start only
s3://bucket/<StartupScriptS3Path>   -> /usr/local/airflow/startup/startup.sh            on container start only

container start: copy files -> source startup.sh (bash, 4 min 55 s) -> pip install (9 min) -> Airflow
packages go to /usr/local/airflow/.local/lib/python3.12/site-packages; scratch space is /tmp (per container)
```

## Rules

### Uploading

1. Expect only `dags/` to update live. `plugins.zip`, `requirements.txt` and `startup.sh` apply only after an environment update that names the new S3 object version (console **Edit**, choose the version, Save; or `update-environment --plugins-s3-object-version "$VERSION"`, from `aws s3api head-object ... --query VersionId`).
2. Upload every file that `requirements.txt` or `startup.sh` reads (a constraints file in `dags/`, wheels in `plugins.zip`) before starting the update; containers copy them before pip runs.
3. Zip the contents of the plugins folder, not the folder: `cd plugins && zip -r ../plugins.zip . -x '*__pycache__*'`. `zip -r plugins.zip plugins` extracts to `/usr/local/airflow/plugins/plugins/`, plugins still register, and imports fail with `ModuleNotFoundError`.
4. To remove `plugins.zip` or `requirements.txt` from an environment, point it at an empty zip or text file; there is no remove. Deleting the current S3 version and updating fails with `Unable to read version ... of file ...`.

### Imports

5. Import shared code from `dags/` or `plugins.zip` by top-level name (`from acme_pipelines.db import x`): Airflow appends `/usr/local/airflow/plugins` and then `/usr/local/airflow/dags` to `sys.path`.
6. Give that code a distinctive package name. Standard-library names (`email`, `logging`), installed packages (`utils`) and MWAA's own `mwaa` (on `PYTHONPATH`) win over yours, and `plugins/` wins over `dags/` for the same name.
7. Do not put `airflow_local_settings.py` in `plugins.zip`; MWAA's `/usr/local/airflow/config/airflow_local_settings.py` comes first on `sys.path`.

### requirements.txt

08. Write the constraint as `--constraint "/usr/local/airflow/dags/<file>"`: absolute, with a space after the option. MWAA detects a constraint with the regex `-c |--constraint `; `--constraint=...` or `-c/path` is missed, so MWAA also adds Apache's `-c airflow_constraints.txt` and pip must satisfy both.
09. Never leave a commented-out `--constraint` line, or a comment containing `-c `, unless you mean it: it matches the regex and switches Apache's constraints off.
10. Never use relative paths in `-c`, `-r` or `--find-links`: pip resolves them against `/usr/local/airflow/requirements/`, which holds only `requirements.txt`, and stops with `Could not open constraint file`.
11. Do not pin anything in MWAA's essential constraints (`apache-airflow`, `Flask*`, `celery`, `kombu`, `SQLAlchemy`, `watchtower`, ...) to another version; `safe-pip-install` always passes `--constraint mwaa_essential_constraints.txt`, and no constraints file of yours overrides it.
12. Put wheels for `--find-links /usr/local/airflow/plugins` with `--no-index` at the top level of `plugins.zip`; pip does not search subdirectories. Build them with the image's `./run.sh package-requirements`.
13. Reference secrets as `${UPPER_CASE}` in `requirements.txt` and `export` them in `startup.sh` (or set an Airflow configuration option, `${AIRFLOW__SECTION__KEY}`); pip leaves an unset `${VAR}` in the line literally.
14. Judge an install by its log, not its status: when pip fails, MWAA logs `ERROR: pip installation exited with a non-zero error code` and starts Airflow anyway, so the symptom is `Broken DAG: No module named ...`; `./run.sh test-requirements` exits 0 either way.

### startup.sh

15. Write it for bash and `export` every variable that tasks or pip need: MWAA `source`s it with bash (the `#!` line is ignored) and keeps only exported variables, dumped with `jq -n env` when the script ends or calls `exit`.
16. Never use `set -e`, `set -o errexit` or `set -euo pipefail`. A failing command then exits before the environment is saved, and the container dies with `Failed to locate environment variables file from startup script`. Test commands with `if ! ...; then echo ...; fi`.
17. Do not rely on `exit 1` to fail a deploy; the status is discarded and the script counts as successful.
18. Do not export `PYTHONPATH`, `AIRFLOW_HOME`, `AWS_REGION`, any `MWAA__*` variable or other reserved names; MWAA restores its own value. Airflow configuration options set in the console override an `AIRFLOW__X__Y` export.
19. Keep it idempotent and under 4 min 55 s: it runs on every container start (each update, each worker scale-out) on scheduler, worker and webserver; branch on `$MWAA_AIRFLOW_COMPONENT`.

## Debug procedure

1. Check the bundle before uploading: `uv run scripts/check_mwaa_layout.py --dags dags --plugins plugins.zip --requirements requirements.txt --startup startup.sh` (from this skill's directory, with paths to your files). Fix every `PROBLEM`.
2. List what the zip will extract: `unzip -l plugins.zip | head -20`. The first path component must not be a lone `plugins/` folder.
3. Confirm the environment points at the versions you uploaded: `aws mwaa get-environment --name "$ENV_NAME" --query 'Environment.[PluginsS3ObjectVersion,RequirementsS3ObjectVersion,StartupScriptS3ObjectVersion,Status]'` and compare with `aws s3api list-object-versions --bucket "$BUCKET" --prefix requirements.txt --query 'Versions[?IsLatest].VersionId'`.
4. Read the CloudWatch log streams: `startup_script_execution...` for the startup script, `requirements_install_...` in the scheduler and worker log groups for pip. Search for `ERROR`, `Forcing local constraints`, `Could not open` and `ResolutionImpossible`.
5. Resolve the requirements against MWAA's constraints without deploying: the `uv pip compile` recipe in `references/requirements-install.md`, section 8.
6. See the real container: deploy the inspection DAG in `references/container-filesystem.md`, section 7, trigger it, and read its task log (paths, `sys.path`, installed packages, disk).
7. Reproduce locally with the image: `./run.sh test-startup-script` and `./run.sh test-requirements` in `images/airflow/<version>/` of amazon-mwaa-docker-images; read the output for `ERROR`.

## Script

`scripts/check_mwaa_layout.py` checks a deploy bundle on your machine, before upload: it prints where each file will land in the container and reports what MWAA will break on. It reads the files only; nothing is run or uploaded. Run it from the bundle's directory:

```bash
uv run <skill-dir>/scripts/check_mwaa_layout.py --dags dags --plugins plugins.zip \
  --requirements requirements.txt --startup startup.sh
```

Pass any subset of the four flags; `--plugins` takes the zip or the folder you zip. Output for a bundle with a nested `plugins.zip` and `set -e` in `startup.sh` (shortened):

```text
plugins.zip -> /usr/local/airflow/plugins/
    /usr/local/airflow/plugins/plugins/my_plugin.py
  PROBLEM: everything is inside 'plugins/', so it extracts to /usr/local/airflow/plugins/plugins/ and 'from <module> import ...' fails with ModuleNotFoundError; zip the folder's contents instead: cd plugins && zip -r ../plugins.zip .

startup.sh -> /usr/local/airflow/startup/startup.sh
  PROBLEM: line 2: 'set -euo pipefail': a failing command then ends the script before MWAA saves its environment, and the container exits with 'Failed to access customer environment variables file'
```

`PROBLEM` lines break the deploy; `WARNING` lines are likely mistakes; `note` lines are information. Exit status is 1 when there is a `PROBLEM`, so it can gate a CI step before `aws s3 cp`. `--help` lists every check. It does not detect clashes with installed package names.

## References

- `references/container-filesystem.md`: the S3-to-container map with diagrams, the two update lifecycles and the start-up sequence, the full `/usr/local/airflow` tree, `sys.path` order with Airflow source, the `plugins.zip` nesting trap, where to put files that other uploads point at, and an inspection DAG. Read it to understand or explain where any file is and when it changes.
- `references/requirements-install.md`: the exact pip command lines, MWAA's constraint-detection regex and its traps, the essential constraints for 3.0.6, pip's relative-path and `${VAR}` rules with source, offline wheels, failure behaviour and limits, and a tested `uv pip compile` pre-flight for CI. Read it when an install fails or before writing a constraints file.
- `references/startup-script.md`: the bash wrapper that sources `startup.sh`, an experiment table of what survives it, why `set -e` is fatal, the variable precedence layers and reserved names, time limits and logs, and working patterns (OS packages, binaries from `plugins.zip`, private git dependencies). Read it before writing or debugging a startup script.
