"""A throwaway local Postgres for testing migration 0013's SQL functions.

The fake Supabase client in tests/conftest.py cannot run SQL, and the live
database must never be written to, so approve_submission() and
admin_update_product() are proven against a local replica instead:

  1. initdb a fresh cluster in a temporary directory and start it on a
     non-default port (54329), listening on localhost only;
  2. create the Supabase roles and tables 0013 touches (replica_schema.sql);
  3. apply app/db/migrations/0013_product_submissions.sql TWICE, which proves it
     can be re-run.

Postgres is driven with psql.exe through subprocess: the app's environment has
no Postgres driver, and nothing is installed for these tests.

These tests SKIP unless both of these are present, so a normal `pytest` run on a
machine without Postgres stays green:
  * PostgreSQL 17 or newer binaries (initdb, pg_ctl, psql), found through the
    SKINBUDDY_PG_BIN environment variable or on PATH. 17+ because the cluster
    uses the builtin C.UTF-8 locale, so lower() folds non-ASCII letters the way
    Python's str.lower() does for the names tested.
  * The migration file. app/db/migrations/ is local-only (never committed), so
    a fresh clone does not have it. SKINBUDDY_MIGRATION_0013 can point elsewhere.

Optional: SKINBUDDY_PG_PORT (default 54329) and SKINBUDDY_PG_DATA (a directory
for the cluster; it is wiped and recreated only if it holds this harness's
marker file, or does not exist).

Run them:
    SKINBUDDY_PG_BIN=<postgres bin dir> python -m pytest -p no:cacheprovider tests/postgres
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path



REPO_ROOT = Path(__file__).resolve().parents[2]
REPLICA_SCHEMA = Path(__file__).with_name("replica_schema.sql")
MIGRATION_0013 = Path(os.environ.get(
    "SKINBUDDY_MIGRATION_0013",
    REPO_ROOT / "app" / "db" / "migrations" / "0013_product_submissions.sql"))
MARKER = ".skinbuddy-test-cluster"
DB_NAME = "skinbuddy_test"
EXE = ".exe" if os.name == "nt" else ""


def _find_bin_dir():
    configured = os.environ.get("SKINBUDDY_PG_BIN")
    if configured:
        return Path(configured)
    found = shutil.which("pg_ctl")
    return Path(found).parent if found else None


def _skip_reason():
    bin_dir = _find_bin_dir()
    if bin_dir is None:
        return "PostgreSQL binaries not found (set SKINBUDDY_PG_BIN)"
    for tool in ("initdb", "pg_ctl", "psql"):
        if not (bin_dir / f"{tool}{EXE}").exists():
            return f"{tool} not found in {bin_dir}"
    out = subprocess.run([str(bin_dir / f"initdb{EXE}"), "--version"],
                         capture_output=True, text=True).stdout
    match = re.search(r"(\d+)", out)
    if not match or int(match.group(1)) < 17:
        return f"PostgreSQL 17+ needed for the builtin C.UTF-8 locale, found: {out.strip()}"
    if not MIGRATION_0013.exists():
        return f"migration not found: {MIGRATION_0013} (app/db/migrations/ is local-only)"
    return None


SKIP_REASON = _skip_reason()


@dataclass
class SqlResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self):
        return self.returncode == 0

    @property
    def sqlstate(self):
        """The SQLSTATE of the error psql reported (VERBOSITY verbose prints it)."""
        match = re.search(r"ERROR:\s+([0-9A-Z]{5}):", self.stderr)
        return match.group(1) if match else None

    @property
    def message(self):
        match = re.search(r"ERROR:\s+[0-9A-Z]{5}:\s*(.*)", self.stderr)
        return match.group(1).strip() if match else None

    @property
    def detail(self):
        match = re.search(r"DETAIL:\s+(.*?)(?:\n[A-Z]+:|\Z)", self.stderr, re.S)
        return match.group(1).strip() if match else None

    def json(self):
        """The last non-empty output line, parsed as JSON."""
        lines = [line for line in self.stdout.splitlines() if line.strip()]
        return json.loads(lines[-1])


def lit(value):
    """A SQL literal for a Python value (None -> NULL)."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


class LocalPostgres:
    def __init__(self, bin_dir: Path, data_dir: Path, port: int):
        self.bin_dir = bin_dir
        self.data_dir = data_dir
        self.port = port
        self.log = data_dir.parent / f"{data_dir.name}.log"
        self.migration_runs = []

    def _tool(self, name):
        return str(self.bin_dir / f"{name}{EXE}")

    def _env(self):
        env = dict(os.environ)
        env.update({"PGCLIENTENCODING": "UTF8", "PGTZ": "UTC"})
        env.pop("PGPASSWORD", None)
        return env

    def start(self):
        if self.data_dir.exists():
            if not (self.data_dir / MARKER).exists():
                raise RuntimeError(f"{self.data_dir} exists and is not a test cluster; refusing to wipe it")
            shutil.rmtree(self.data_dir)
        self.data_dir.mkdir(parents=True)
        (self.data_dir / MARKER).write_text("created by tests/postgres/pg_harness.py\n")
        # initdb wants an empty directory: build it alongside, then move the marker in.
        cluster = self.data_dir / "cluster"
        subprocess.run(
            [self._tool("initdb"), "-D", str(cluster), "-U", "postgres", "-A", "trust",
             "-E", "UTF8", "--locale-provider=builtin", "--builtin-locale=C.UTF-8"],
            check=True, capture_output=True, env=self._env())
        # No pipes here: the server pg_ctl starts inherits them and never closes
        # them, so capturing output would wait forever. Its log goes to -l instead.
        subprocess.run(
            [self._tool("pg_ctl"), "start", "-D", str(cluster), "-w", "-l", str(self.log),
             "-o", f"-p {self.port} -c listen_addresses=localhost -c fsync=off"],
            check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=self._env())
        self.cluster = cluster

    def stop(self):
        subprocess.run([self._tool("pg_ctl"), "stop", "-D", str(self.cluster), "-m", "fast", "-w"],
                       capture_output=True, env=self._env())

    def run(self, sql, *, db=DB_NAME, role=None) -> SqlResult:
        """Run SQL in a fresh psql session. Each statement commits on its own."""
        if role:
            sql = f"set role {role};\n{sql}"
        proc = subprocess.run(
            [self._tool("psql"), "-X", "-q", "-A", "-t", "-h", "localhost", "-p", str(self.port),
             "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-f", "-"],
            input=sql.encode("utf-8"), capture_output=True, env=self._env())
        return SqlResult(proc.returncode, proc.stdout.decode("utf-8", "replace"),
                         proc.stderr.decode("utf-8", "replace"))

    def run_file(self, path: Path, *, db=DB_NAME) -> SqlResult:
        return self.run(Path(path).read_text(encoding="utf-8"), db=db)

    def check(self, sql, **kwargs) -> SqlResult:
        result = self.run(sql, **kwargs)
        assert result.ok, result.stderr
        return result

    def create_database(self, name):
        self.check(f"drop database if exists {name};", db="postgres")
        self.check(f"create database {name};", db="postgres")
        self.check(REPLICA_SCHEMA.read_text(encoding="utf-8"), db=name)
