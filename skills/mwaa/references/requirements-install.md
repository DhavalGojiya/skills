# How MWAA Installs requirements.txt

What actually runs when an MWAA container installs your `requirements.txt`: the pip command line, the two constraint files MWAA adds around yours, how it decides whether you supplied a constraint, where pip looks for files your requirements name, and what happens when the install fails.

Verified against the MWAA image source, [amazon-mwaa-docker-images](https://github.com/aws/amazon-mwaa-docker-images/tree/32b2a2e81c99fa8b937dcb2eead71c9b9252127f) at commit `32b2a2e` (`images/airflow/3.0.6`; `user_requirements.py` and `safe-pip-install` are identical in the `2.10.3` and `2.11.2` directories and differ by one logging flag in `3.2.1` and `3.3.1`), pip [26.2.1](https://github.com/pypa/pip/tree/26.2.1), uv 0.13.1, and the Amazon MWAA User Guide as published on 2026-10-15. Where the container paths come from is in `container-filesystem.md`.

______________________________________________________________________

## Table of Contents

1. [The Command MWAA Runs](#1-the-command-mwaa-runs)
2. [How MWAA Decides You Have a Constraint](#2-how-mwaa-decides-you-have-a-constraint)
3. [The Essential Constraints You Cannot Override](#3-the-essential-constraints-you-cannot-override)
4. [Relative Paths Resolve Against /usr/local/airflow/requirements](#4-relative-paths-resolve-against-usrlocalairflowrequirements)
5. [Environment Variables in requirements.txt](#5-environment-variables-in-requirementstxt)
6. [Offline Installs from plugins.zip](#6-offline-installs-from-pluginszip)
7. [When the Install Fails](#7-when-the-install-fails)
8. [Checking Before You Upload](#8-checking-before-you-upload)

______________________________________________________________________

## 1. The Command MWAA Runs

The User Guide says MWAA "runs `pip3 install -r requirements.txt`". The image runs more than that:

```mermaid
flowchart TD
    A["install_user_requirements()<br/>MWAA__CORE__REQUIREMENTS_PATH =<br/>/usr/local/airflow/requirements/requirements.txt"] --> B{"regex '-c |--constraint '<br/>matches any line?"}
    B -->|yes| C["extra_args = []"]
    B -->|no| D["log 'Forcing local constraints'<br/>extra_args = -c $AIRFLOW_CONSTRAINTS_FILE"]
    C --> E["safe-pip-install -r requirements.txt"]
    D --> E2["safe-pip-install -r requirements.txt<br/>-c /usr/local/airflow/.local/etc/airflow_constraints.txt"]
    E --> F["pip3 install<br/>--constraint /usr/local/airflow/.local/etc/mwaa_essential_constraints.txt<br/>-r ... (+ your own --constraint inside the file)"]
    E2 --> F
    F --> G{"exit status"}
    G -->|0| H["start Airflow"]
    G -->|non-zero| I["log 'ERROR: pip installation exited with a non-zero error code...'<br/>start Airflow anyway"]
```

Source: [`images/airflow/3.0.6/python/mwaa/utils/user_requirements.py` L80-L98](https://github.com/aws/amazon-mwaa-docker-images/blob/32b2a2e81c99fa8b937dcb2eead71c9b9252127f/images/airflow/3.0.6/python/mwaa/utils/user_requirements.py#L80-L98)

```python
        extra_args = []
        try:
            if not _requirements_has_constraints(requirements_file):
                subprocess_logger.warning(
                    "WARNING: Constraints should be specified for requirements.txt. "
                    f"Please see {MWAA_DOCS_REQUIREMENTS_GUIDE}"
                )
                subprocess_logger.warning("Forcing local constraints")
                extra_args = ["-c", os.environ["AIRFLOW_CONSTRAINTS_FILE"]]
        except Exception as e:
            subprocess_logger.warning(f"Unable to scan requirements file: {e}")
            subprocess_logger.warning(
                "Cannot determine whether the requirements.txt file has constraints "
                "or not; forcing local constraints."
            )
            extra_args = ["-c", os.environ["AIRFLOW_CONSTRAINTS_FILE"]]

        pip_process = Subprocess(
            cmd=["safe-pip-install", "-r", requirements_file, *extra_args],
```

`safe-pip-install` is a one-line wrapper that always adds MWAA's essential constraints:

Source: [`images/airflow/3.0.6/bin/airflow-user/safe-pip-install` L8](https://github.com/aws/amazon-mwaa-docker-images/blob/32b2a2e81c99fa8b937dcb2eead71c9b9252127f/images/airflow/3.0.6/bin/airflow-user/safe-pip-install#L8)

```bash
pip3 install --constraint "${MWAA_ESSENTIAL_CONSTRAINTS_FILE}" "$@"
```

Both constraint files are baked into the image:

Source: [`images/airflow/3.0.6/Dockerfiles/Dockerfile.base` L41-L42](https://github.com/aws/amazon-mwaa-docker-images/blob/32b2a2e81c99fa8b937dcb2eead71c9b9252127f/images/airflow/3.0.6/Dockerfiles/Dockerfile.base#L41-L42)

```dockerfile
ENV AIRFLOW_CONSTRAINTS_FILE=${AIRFLOW_USER_LOCAL_ETC_PATH}/airflow_constraints.txt
ENV MWAA_ESSENTIAL_CONSTRAINTS_FILE=${AIRFLOW_USER_LOCAL_ETC_PATH}/mwaa_essential_constraints.txt
```

`AIRFLOW_USER_LOCAL_ETC_PATH` is `/usr/local/airflow/.local/etc`. So every install is one of these two command lines:

```text
your file has a constraint line (as MWAA detects it, section 2):
  pip3 install --constraint /usr/local/airflow/.local/etc/mwaa_essential_constraints.txt \
               -r /usr/local/airflow/requirements/requirements.txt

your file has none:
  pip3 install --constraint /usr/local/airflow/.local/etc/mwaa_essential_constraints.txt \
               -r /usr/local/airflow/requirements/requirements.txt \
               -c /usr/local/airflow/.local/etc/airflow_constraints.txt
```

pip runs as the `airflow` user with the environment `startup.sh` produced, and installs into `/usr/local/airflow/.local/lib/python3.12/site-packages`, where Airflow and its providers already are. A requirement that is already satisfied is left alone; one that needs a different version replaces the installed copy, unless a constraint forbids it.

______________________________________________________________________

## 2. How MWAA Decides You Have a Constraint

It does not parse the file the way pip does. It searches each raw line for a regular expression:

Source: [`images/airflow/3.0.6/python/mwaa/utils/user_requirements.py` L37-L46](https://github.com/aws/amazon-mwaa-docker-images/blob/32b2a2e81c99fa8b937dcb2eead71c9b9252127f/images/airflow/3.0.6/python/mwaa/utils/user_requirements.py#L37-L46)

```python
def _requirements_has_constraints(requirements_file: str):
    content = _read_requirements_file(requirements_file)
    for line in content.splitlines():
        # Notice that this regex check will also match lines with commented out
        # constraints flag. This is intentional as a mechanism for users who want to
        # avoid enforcing the default Airflow constraints, yet don't want to provide a
        # constraints file.
        if re.search(r"-c |--constraint ", line):
            return True
    return False
```

The regex needs a literal space after `-c` or `--constraint`. Tested with Python's `re` on the exact pattern:

| Line in `requirements.txt`                               | MWAA sees a constraint | What pip then applies                                                  |
| -------------------------------------------------------- | ---------------------- | ---------------------------------------------------------------------- |
| `--constraint "/usr/local/airflow/dags/constraints.txt"` | yes                    | essential + yours                                                      |
| `-c /usr/local/airflow/dags/constraints.txt`             | yes                    | essential + yours                                                      |
| `--constraint=/usr/local/airflow/dags/constraints.txt`   | **no**                 | essential + yours + Apache's, which can contradict yours               |
| `-c/usr/local/airflow/dags/constraints.txt`              | **no**                 | essential + yours + Apache's                                           |
| `# --constraint "https://..."` (commented out)           | yes                    | essential only: Apache's constraints are skipped                       |
| `requests==2.32.5  # pinned, see -c docs`                | yes                    | essential only; a comment that happens to contain `-c ` turns them off |
| no constraint at all                                     | no                     | essential + Apache's, and the log says `Forcing local constraints`     |

So write the option as `--constraint "<path>"` with a space, and expect that a commented-out constraint line is an opt-out, not a no-op.

Why the `=` form hurts: with two constraint files, pip must satisfy both. If yours raises a provider that Apache's file pins lower, the install fails with `ResolutionImpossible`, even though your file alone is consistent.

______________________________________________________________________

## 3. The Essential Constraints You Cannot Override

`mwaa_essential_constraints.txt` is always applied, whatever your file says. For the 3.0.6 image it pins:

Source: [`images/airflow/3.0.6/etc/mwaa_essential_constraints.txt` L10-L40](https://github.com/aws/amazon-mwaa-docker-images/blob/32b2a2e81c99fa8b937dcb2eead71c9b9252127f/images/airflow/3.0.6/etc/mwaa_essential_constraints.txt#L10-L40)

```text
apache-airflow==3.0.6
# Webserver-related packages
Flask-AppBuilder==4.6.3
Flask-Babel==2.0.0
Flask-Bcrypt==1.0.1
Flask-Caching==2.3.0
Flask-JWT-Extended==4.7.1
Flask-Limiter==3.12
Flask-Login==0.6.3
Flask-SQLAlchemy==2.5.1
Flask-Session==0.5.0
Flask-WTF==1.2.2
Flask==2.2.5
gunicorn==23.0.0
# Celery and related packages
billiard==4.2.1
celery==5.5.2
gevent==25.5.1
greenlet==3.2.4
kombu==5.5.4
vine==5.1.0
# SQLAlchemy
SQLAlchemy-JSONField==1.0.2
SQLAlchemy-Utils==0.41.2
SQLAlchemy==1.4.54
# StatsD
statsd==4.0.1
# Watchtower
watchtower==3.4.0
# Fluent Logger
fluent-logger==0.11.1
```

A requirement that needs any of these at another version (for example a library that requires `SQLAlchemy>=2`, or `Flask>=3`) cannot install on MWAA 3.0.6, whatever constraints file you write. The only fix is a library version compatible with the pins, or a different Airflow version. Find the file for another version under `images/airflow/<version>/etc/` in the same repository.

The User Guide's provider page says MWAA "installs Watchtower version 2.0.1 after performing `pip3 install -r requirements.txt`"; the 3.0.6 image instead pins `watchtower==3.4.0` here and checks for it in `post-startup-script-verification`.

Everything else Airflow ships is pinned only by Apache's `airflow_constraints.txt`, which you replace when you supply your own constraint. A custom constraints file is the supported way to move a provider or library past Apache's pin: copy Apache's file for your Airflow and Python versions, edit the lines you need, put it in `dags/`, and reference it by absolute path (section 4).

______________________________________________________________________

## 4. Relative Paths Resolve Against /usr/local/airflow/requirements

pip resolves a nested `-r` or `-c` path against the directory of the file that contains it, not against the current directory:

Source: [`src/pip/_internal/req/req_file.py` L368-L380](https://github.com/pypa/pip/blob/634a6ec1a5d9dcc2433571cdb2f4c58a4bb29caf/src/pip/_internal/req/req_file.py#L368-L380)

```python
                # original file is over http
                if SCHEME_RE.search(filename):
                    # do a url join so relative paths work
                    req_path = urllib.parse.urljoin(filename, req_path)
                # original file and nested file are paths
                elif not SCHEME_RE.search(req_path):
                    # do a join so relative paths work
                    # and then abspath so that we can identify recursive references
                    req_path = os.path.abspath(
                        os.path.join(
                            os.path.dirname(filename),
                            req_path,
                        )
                    )
```

`--find-links` with a local path is tried relative to the requirements file first, and used as given only if that does not exist ([`req_file.py` L247-L256](https://github.com/pypa/pip/blob/634a6ec1a5d9dcc2433571cdb2f4c58a4bb29caf/src/pip/_internal/req/req_file.py#L247-L256)).

On MWAA `filename` is `/usr/local/airflow/requirements/requirements.txt`, so:

| Line                                                     | pip opens                                              | Result                                  |
| -------------------------------------------------------- | ------------------------------------------------------ | --------------------------------------- |
| `--constraint "constraints.txt"`                         | `/usr/local/airflow/requirements/constraints.txt`      | file not found                          |
| `--constraint "dags/constraints.txt"`                    | `/usr/local/airflow/requirements/dags/constraints.txt` | file not found                          |
| `--constraint "../dags/constraints.txt"`                 | `/usr/local/airflow/dags/constraints.txt`              | works, but depends on the internal path |
| `--constraint "/usr/local/airflow/dags/constraints.txt"` | `/usr/local/airflow/dags/constraints.txt`              | works; the documented form              |
| `-r /usr/local/airflow/dags/requirements-extra.txt`      | that path                                              | works                                   |
| `--find-links /usr/local/airflow/plugins`                | that directory                                         | works                                   |

A relative path that works on your laptop (where `requirements.txt` sits beside `dags/`) fails on MWAA. A failed constraint file read stops pip before it installs anything, with:

```text
ERROR: Could not open constraint file: [Errno 2] No such file or directory: '/usr/local/airflow/requirements/constraints.txt'
```

The file a constraint names must be in S3 before the environment update starts (`container-filesystem.md`, section 2): upload it to `dags/` in the same deploy step as `requirements.txt`, not after.

______________________________________________________________________

## 5. Environment Variables in requirements.txt

pip expands `${NAME}` in requirements files from its own environment:

Source: [`src/pip/_internal/req/req_file.py` L552-L558](https://github.com/pypa/pip/blob/634a6ec1a5d9dcc2433571cdb2f4c58a4bb29caf/src/pip/_internal/req/req_file.py#L552-L558)

```python
    for line_number, line in lines_enum:
        for env_var, var_name in ENV_VAR_RE.findall(line):
            value = os.getenv(var_name)
            if not value:
                continue

            line = line.replace(env_var, value)
```

`ENV_VAR_RE` is `\$\{(?P<name>[A-Z0-9_]+)\}`: braces required, uppercase letters, digits and `_` only. An unset or empty variable is left in the line literally, so a URL with `${TOKEN}` in it reaches the index as-is and fails authentication.

pip's environment on MWAA is the one `startup.sh` produced, so a token reaches `requirements.txt` in one of two ways:

| Source of the value                                     | Name in `requirements.txt` |
| ------------------------------------------------------- | -------------------------- |
| `export PRIVATE_PYPI_TOKEN=...` in `startup.sh`         | `${PRIVATE_PYPI_TOKEN}`    |
| Airflow configuration option `foo.token` in the console | `${AIRFLOW__FOO__TOKEN}`   |

```text
--index-url https://__token__:${PRIVATE_PYPI_TOKEN}@pypi.example.com/simple
my-private-package==1.2.3
```

The variable must be **exported**; a plain assignment in `startup.sh` never leaves the script (`startup-script.md`). Private `git+https` dependencies work the same way without putting the token in any file: export `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` and `GIT_CONFIG_VALUE_0` in `startup.sh` so that git rewrites `https://github.com/` to a URL with the token.

______________________________________________________________________

## 6. Offline Installs from plugins.zip

For environments without internet access (private webserver, no NAT), ship wheels in `plugins.zip` and point pip at them. `plugins.zip` is extracted before pip runs.

```text
--find-links /usr/local/airflow/plugins
--no-index
numpy==2.1.0
```

- Put the wheels at the **top level** of the zip. pip lists a `--find-links` directory with `os.scandir()`, which does not recurse ([`src/pip/_internal/index/sources.py` L62](https://github.com/pypa/pip/blob/634a6ec1a5d9dcc2433571cdb2f4c58a4bb29caf/src/pip/_internal/index/sources.py#L62)).
- Build the wheels on the MWAA image, not your laptop, so that compiled wheels match Amazon Linux 2023, x86_64 and Python 3.12. The image has a command for exactly this: `./run.sh package-requirements` runs `pip3 download -r` on your requirements into `requirements/downloads/` and writes `requirements/plugins.zip` plus `requirements/packaged_requirements.txt`, which is your file prefixed with `--no-index` and `--find-links /usr/local/airflow/plugins` ([`user_requirements.py` L119-L177](https://github.com/aws/amazon-mwaa-docker-images/blob/32b2a2e81c99fa8b937dcb2eead71c9b9252127f/images/airflow/3.0.6/python/mwaa/utils/user_requirements.py#L119-L177)).
- That generated zip holds only wheels; merge your plugin files into it before uploading if you have both.

______________________________________________________________________

## 7. When the Install Fails

The image does not stop the container when pip fails:

Source: [`images/airflow/3.0.6/python/mwaa/utils/user_requirements.py` L106-L114](https://github.com/aws/amazon-mwaa-docker-images/blob/32b2a2e81c99fa8b937dcb2eead71c9b9252127f/images/airflow/3.0.6/python/mwaa/utils/user_requirements.py#L106-L114)

```python
        pip_process.start()
        if pip_process.process and pip_process.process.returncode != 0:
            subprocess_logger.error(
                "ERROR: pip installation exited with a non-zero error code. This could "
                "be the result of package conflict. Notice that MWAA enforces a list "
                "of critical packages, e.g. Airflow, Celery, among others, whose "
                "version cannot be overridden by the customer as that can break our "
                "setup. Please double check your requirements.txt file."
            )
```

Execution continues to the Airflow command. pip resolves the whole file before installing anything, so a resolution failure installs none of your packages, and the symptom moves to the Airflow UI: `Broken DAG: ... No module named 'x'`.

| Limit or behaviour                       | Value                                                                                                    | Source                                                           |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| pip time limit in the image              | 9 minutes, then the process is stopped                                                                   | `USER_REQUIREMENTS_MAX_INSTALL_TIME`, `user_requirements.py` L27 |
| service-side limit                       | 10 minutes, then MWAA "will time-out and attempt to rollback the environment to a stable state"          | User Guide, "Installing Python dependencies"                     |
| where the pip output goes                | log stream `requirements_install_ip...` in the scheduler log group (and the worker and webserver groups) | User Guide; image logger `{cmd}_requirements`                    |
| `./run.sh test-requirements` exit status | 0 even when pip failed; it prints `Finished testing requirements` either way                             | `entrypoint.py` L275-L279                                        |

So read the log, not the exit status. Search the requirements log stream, or the local runner's output, for `ERROR`:

```bash
./run.sh test-requirements 2>&1 | tee test-requirements.log
grep -E 'ERROR|Forcing local constraints|ResolutionImpossible|Could not open' test-requirements.log
```

The User Guide recommends scheduler logs at `INFO` "to access logs received for the `requirements.txt`".

______________________________________________________________________

## 8. Checking Before You Upload

Three checks, from fastest to most faithful:

1. **Static check.** This skill's checker reports a constraint line MWAA will not detect, relative paths, files referenced under `/usr/local/airflow/dags` or `/plugins` that are missing locally, `${VARS}` that `startup.sh` does not export, and pins of essential packages:

   ```bash
   uv run scripts/check_mwaa_layout.py --dags dags --plugins plugins.zip \
     --requirements requirements.txt --startup startup.sh
   ```

2. **Resolve without installing.** Rewrite the container paths to your checkout, then resolve Airflow plus your requirements under the image's essential constraints with uv, for Linux x86_64 and Python 3.12. Fetch the constraint files for your Airflow version from the image repository:

   ```bash
   MWAA_ETC=https://raw.githubusercontent.com/aws/amazon-mwaa-docker-images/32b2a2e81c99fa8b937dcb2eead71c9b9252127f/images/airflow/3.0.6/etc
   curl -fsSLO "$MWAA_ETC/mwaa_essential_constraints.txt"
   curl -fsSLO "$MWAA_ETC/airflow_constraints.txt"
   sed "s#/usr/local/airflow/dags/#$PWD/dags/#g; s#/usr/local/airflow/plugins#$PWD/plugins#g" \
     requirements.txt > requirements-ci.txt
   printf '%s\n' 'apache-airflow[celery,statsd]==3.0.6' \
     'apache-airflow-providers-amazon[aiobotocore]' 'apache-airflow-providers-postgres' \
     'apache-airflow-providers-standard' > mwaa-base.txt
   uv pip compile --python-version 3.12 --python-platform x86_64-unknown-linux-gnu \
     mwaa-base.txt requirements-ci.txt -c mwaa_essential_constraints.txt -o resolved.txt
   ```

   If your `requirements.txt` has no constraint line, add `-c airflow_constraints.txt` to match what MWAA forces. `mwaa-base.txt` stands in for what the image installs before your file (its `REQUIRED_PACKAGES` in `bootstrap/02-airflow/001-install-required-pip-packages.sh`); without `apache-airflow==3.0.6` in the resolve, an unpinned provider can pull a newer Airflow. A conflict prints `No solution found when resolving dependencies` with the chain of requirements that caused it.

   Do not validate Apache's constraints file on its own (`uv pip compile constraints.txt`): it is a list of pins for one install, not a consistent set. Apache's 3.0.6 file pins `diagrams==0.24.4`, which requires `graphviz<0.21.0`, next to `graphviz==0.21`, so compiling it alone fails although every real install with it succeeds, because a constraint applies only to a package that something requires.

3. **Install in the MWAA image.** `./run.sh test-requirements` from `images/airflow/<version>/` in [amazon-mwaa-docker-images](https://github.com/aws/amazon-mwaa-docker-images), with your files at `requirements/requirements.txt`, `startup/startup.sh` and `dags/`. It runs your startup script first, exactly as MWAA does, then the same `safe-pip-install` command. Read the log (section 7).
