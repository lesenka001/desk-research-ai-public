"""Private Docker release snapshots. Standard library only; no import-time I/O.

The source-only SPEC003 snapshot is deliberately a different, unsupported format.
Docker and SQL are invoked only by capture()/rollback(), never by validation.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SERVICES = ("worker", "bot", "web")
KIND = "deployed-docker-release"
FORMAT_VERSION = 1
ARTIFACTS = frozenset({
    "compose.resolved.json", "compose.rollback.json", "deployment.json",
    "database.json", "images.tar",
})
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
SENTINEL = re.compile(r"__release_no_baseline_[0-9a-f]{32}__\Z")


class ReleaseError(RuntimeError):
    """A safe, credential-free diagnostic for the CLI."""


def run_command(args, input_text=None):
    """Single mockable subprocess boundary; return stdout, never echo secrets."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    try:
        result = subprocess.run(
            [str(arg) for arg in args], input=input_text, text=True,
            encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, env=env,
        )
    except OSError:
        raise ReleaseError("Cannot launch Docker; Docker Compose v2 is required.") from None
    if result.returncode:
        # Docker/Compose errors can contain the entire expanded secret config.
        raise ReleaseError("Docker/psql command failed; output withheld (may contain secrets).")
    return result.stdout


def _json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        raise ReleaseError("Invalid JSON in release data or Docker response.") from None


def _require(condition, message):
    if not condition:
        raise ReleaseError(message)


def _private_location(snapshot):
    raw = Path(snapshot).expanduser().absolute()
    # Reject symlinks/junctions in all existing components, not just the leaf.
    for part in (raw, *raw.parents):
        _require(not part.is_symlink() and not getattr(part, "is_junction", lambda: False)(),
                 "Snapshot paths must not contain symlinks or junctions.")
    path = raw.resolve()
    _require(path != REPO_ROOT and REPO_ROOT not in path.parents,
             "Snapshot must be private and outside the repository.")
    _require(not any((parent / ".git").exists() for parent in (path, *path.parents)),
             "Snapshot must be outside every Git working tree.")
    return path


def _private_permissions(path):
    if os.name == "posix":
        mode = path.stat().st_mode
        _require(path.stat().st_uid == os.geteuid() and not mode & 0o077,
                 "Snapshot must be owned by this user with no group/other permissions.")


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        if os.name == "posix":
            os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compose(config, project=None, directory=None, isolated=False):
    args = ["docker", "compose"]
    if isolated:
        # Do not let today's .env change Compose controls or resurrect old variables.
        args += ["--env-file", os.devnull]
    args += ["--project-directory", str(directory or REPO_ROOT), "-f", str(config)]
    if project:
        args += ["--project-name", project]
    return args


def _container(compose, service, allow_missing=False):
    ids = run_command(compose + ["ps", "--all", "--quiet", service]).split()
    if not ids and allow_missing:
        return None
    _require(len(ids) == 1, "Exactly one existing container per service is required.")
    rows = _json(run_command(["docker", "inspect", ids[0]]))
    _require(isinstance(rows, list) and len(rows) == 1, "Invalid container inspection.")
    return rows[0]


def _identity(container, project, service):
    labels = container.get("Config", {}).get("Labels") or {}
    _require(labels.get("com.docker.compose.project") == project
             and labels.get("com.docker.compose.service") == service,
             "Container does not belong to the captured Compose project/service.")


def _pg_mounts(container):
    mounts = container.get("Mounts", [])
    volumes = [m for m in mounts if m.get("Type") == "volume"]
    _require(volumes, "Postgres must use an existing named data volume.")
    _require(any(m.get("Destination", "").startswith("/var/lib/postgresql") for m in volumes),
             "Cannot identify the persistent Postgres data volume.")
    return sorted([
        {key: m.get(key) for key in ("Type", "Name", "Source", "Destination", "RW")}
        for m in mounts
    ], key=lambda m: m["Destination"])


def _pg_database(container):
    env = dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)
    user = env.get("POSTGRES_USER", "postgres")
    return {"user": user, "name": env.get("POSTGRES_DB", user)}


def _check_runtime_env(service_config, container):
    actual = dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)
    expected = service_config.get("environment") or {}
    _require(isinstance(expected, dict), "Compose environment must be fully resolved.")
    for key, value in expected.items():
        # config --format json escapes literal dollars for a future Compose read.
        _require((value is None and key not in actual)
                 or (isinstance(value, str) and actual.get(key) == value.replace("$$", "$")),
                 "Resolved environment differs from the running container.")


def _psql(compose, database, sql):
    return run_command(compose + [
        "exec", "-T", "postgres", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1",
        "-U", database["user"], "-d", database["name"],
    ], input_text=sql)


CAPTURE_SQL = """
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT jsonb_build_object(
  'materials', COALESCE((SELECT jsonb_agg(row_to_json(b) ORDER BY b.id) FROM (
    SELECT m.id, m.url, m.status, m.processing_error, m.processed_prompt_version,
      COALESCE(m.processed_prompt_version,
        (SELECT f.prompt_version FROM findings f WHERE f.material_id = m.id
         ORDER BY f.created_at DESC, f.id DESC LIMIT 1)) AS resolved_prompt_version
    FROM materials m
  ) b), '[]'::jsonb),
  'schema_columns', (SELECT jsonb_agg(row_to_json(c) ORDER BY c.table_name, c.ordinal_position)
    FROM (SELECT table_name, column_name, data_type, is_nullable, ordinal_position
          FROM information_schema.columns
          WHERE table_schema = 'public' AND table_name IN ('materials', 'findings')) c),
  'server_version', current_setting('server_version'),
  'captured_at', transaction_timestamp());
COMMIT;
"""


def _validate_materials(materials):
    _require(isinstance(materials, list), "Missing material baseline.")
    seen = set()
    required = {"id", "url", "status", "processing_error", "processed_prompt_version",
                "resolved_prompt_version"}
    for row in materials:
        _require(isinstance(row, dict) and set(row) == required, "Invalid material record.")
        ident = row["id"]
        _require(type(ident) is int and ident > 0 and ident not in seen,
                 "Invalid or duplicate material ID.")
        seen.add(ident)
        _require(isinstance(row["url"], str) and isinstance(row["status"], str),
                 "Invalid material URL/status.")
        for key in ("processing_error", "processed_prompt_version", "resolved_prompt_version"):
            _require(row[key] is None or isinstance(row[key], str), "Invalid material pointer/error.")
        _require(row["processed_prompt_version"] is None
                 or row["processed_prompt_version"] == row["resolved_prompt_version"],
                 "Resolved pointer disagrees with explicit baseline pointer.")


def _literal(value):
    # E strings make quoting independent of standard_conforming_strings.
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _baseline_sql(materials):
    _validate_materials(materials)
    payload = _literal(json.dumps(materials, ensure_ascii=True))
    return f"""
CREATE TEMP TABLE release_baseline ON COMMIT DROP AS
SELECT * FROM jsonb_to_recordset({payload}::jsonb) AS b(
  id integer, url text, status text, processing_error text,
  processed_prompt_version text, resolved_prompt_version text);
DO $release$
BEGIN
  IF EXISTS (SELECT 1 FROM release_baseline b LEFT JOIN materials m ON m.id = b.id
             WHERE m.id IS NULL OR m.url IS DISTINCT FROM b.url) THEN
    RAISE EXCEPTION 'Material baseline missing or database identity differs';
  END IF;
END $release$;
"""


def build_restore_sql(materials, sentinel):
    """Pure SQL builder. Never deletes findings or restores a nullable pointer."""
    _require(isinstance(sentinel, str) and SENTINEL.fullmatch(sentinel), "Invalid sentinel.")
    quoted = _literal(sentinel)
    return """BEGIN;
SET LOCAL lock_timeout = '30s';
LOCK TABLE materials, findings IN SHARE ROW EXCLUSIVE MODE;
""" + _baseline_sql(materials) + f"""
DO $release$
BEGIN
  IF EXISTS (SELECT 1 FROM findings WHERE prompt_version = {quoted}) THEN
    RAISE EXCEPTION 'Rollback sentinel collides with a findings version';
  END IF;
END $release$;
UPDATE materials m SET
  processed_prompt_version = COALESCE(b.resolved_prompt_version, {quoted}),
  status = CASE WHEN b.resolved_prompt_version IS NULL THEN 'new' ELSE b.status END,
  processing_error = b.processing_error
FROM release_baseline b WHERE m.id = b.id;
UPDATE materials m SET processed_prompt_version = {quoted}, status = 'new',
  processing_error = NULL
WHERE NOT EXISTS (SELECT 1 FROM release_baseline b WHERE b.id = m.id);
COMMIT;
"""


def _pinned_config(config, deployment):
    pinned = copy.deepcopy(config)
    _require(set(pinned.get("services", {})) == {*APP_SERVICES, "postgres"},
             "Only worker/bot/web/postgres projects are supported.")
    _require(not pinned.get("secrets") and not pinned.get("configs"),
             "External secret/config files cannot be made immutable by this snapshot format.")
    for service in APP_SERVICES:
        entry = pinned["services"][service]
        _require(not entry.get("volumes") and not entry.get("volumes_from")
                 and not entry.get("env_file") and not entry.get("develop")
                 and not entry.get("secrets") and not entry.get("configs")
                 and not entry.get("label_file"),
                 "App filesystem/env/config dependencies must be baked in or resolved first.")
        entry.pop("build", None)
        entry["image"] = deployment["apps"][service]["image_id"]
        entry["pull_policy"] = "never"
    # Compose config already escapes every literal $ as $$ (JSON as well as YAML).
    # Preserve that output verbatim: doubling it again would corrupt credentials.
    return pinned


def _validate_archive(path, expected_ids):
    try:
        with tarfile.open(path, "r:") as archive:
            members = archive.getmembers()
            names = set()
            for member in members:
                name = PurePosixPath(member.name)
                _require(not name.is_absolute() and ".." not in name.parts
                         and "\\" not in member.name and member.name not in names
                         and (member.isfile() or member.isdir()), "Unsafe image archive path/type.")
                names.add(member.name)
            manifest = archive.extractfile("manifest.json")
            _require(manifest is not None, "Missing Docker save manifest.")
            entries = _json(manifest.read())
            _require(isinstance(entries, list), "Invalid Docker save manifest.")
            found = set()
            for entry in entries:
                _require(isinstance(entry, dict), "Invalid image manifest entry.")
                config_path = entry.get("Config")
                _require(isinstance(config_path, str) and config_path in names,
                         "Missing image config in archive.")
                layers = entry.get("Layers", [])
                _require(isinstance(layers, list) and all(
                    isinstance(layer, str) and layer in names and archive.getmember(layer).isfile()
                    for layer in layers), "Unsafe or missing image layer path.")
                image_config = archive.extractfile(config_path)
                _require(image_config is not None, "Invalid image config in archive.")
                found.add("sha256:" + hashlib.sha256(image_config.read()).hexdigest())
            _require(found == set(expected_ids), "Archive does not contain exactly the captured images.")
    except (tarfile.TarError, KeyError, OSError):
        raise ReleaseError("Invalid Docker image archive.") from None


def capture(snapshot, compose_file=None, project_name=None):
    """Read deployment, export images and DB pointers into a NEW private directory.

    No Docker/DB mutations. An interrupted capture has no manifest and is unusable.
    """
    path = _private_location(snapshot)
    _require(not path.exists(), "Snapshot already exists; it will never be overwritten.")
    config_path = Path(compose_file or REPO_ROOT / "docker-compose.yml").resolve(strict=True)
    compose = _compose(config_path, project_name, config_path.parent)
    config = _json(run_command(compose + ["config", "--format", "json"]))
    project = config.get("name")
    _require(isinstance(project, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project),
             "Cannot resolve a valid Compose project name.")
    compose = _compose(config_path, project, config_path.parent)
    containers = {service: _container(compose, service) for service in (*APP_SERVICES, "postgres")}
    for service, container in containers.items():
        _identity(container, project, service)
        _require(container.get("State", {}).get("Running"), "Capture requires all four services running.")
        # Catch a changed .env/compose file that has not actually been deployed.
        response = run_command(compose + ["config", "--hash", service]).split()
        config_hash = container["Config"]["Labels"].get("com.docker.compose.config-hash")
        _require(len(response) == 2 and response[0] == service and response[1] == config_hash,
                 "Resolved Compose config differs from the running deployment (or Compose version).")
        _check_runtime_env(config["services"][service], container)
    daemon = run_command(["docker", "info", "--format", "{{.ID}}"]).strip()
    _require(daemon, "Cannot identify Docker daemon.")
    deployment = {
        "daemon_id": daemon, "project_name": project,
        "project_directory": str(config_path.parent), "apps": {},
        "postgres_mounts": _pg_mounts(containers["postgres"]),
        "database": _pg_database(containers["postgres"]),
    }
    for service in APP_SERVICES:
        container = containers[service]
        ident = container["Image"]
        _require(isinstance(ident, str) and IMAGE_ID.fullmatch(ident), "Invalid immutable image ID.")
        metadata = _json(run_command(["docker", "image", "inspect", ident]))[0]
        _require(metadata.get("Id") == ident, "Image inspection differs from container image.")
        deployment["apps"][service] = {
            "container_id": container["Id"], "image_id": ident,
            "repo_digests": metadata.get("RepoDigests") or [],
            "image_labels": metadata.get("Config", {}).get("Labels") or {},
        }
    pinned = _pinned_config(config, deployment)
    database = _json(_psql(compose, deployment["database"], CAPTURE_SQL))
    _validate_materials(database.get("materials"))
    _require(isinstance(database.get("schema_columns"), list), "Missing schema metadata.")
    sentinel = "__release_no_baseline_" + uuid.uuid4().hex + "__"
    _require(_psql(compose, deployment["database"],
                   "SELECT count(*) FROM findings WHERE prompt_version = " + _literal(sentinel) + ";").strip() == "0",
             "Sentinel unexpectedly collides with an existing version.")
    # Private umask also covers the file created by docker image save.
    old_umask = os.umask(0o077)
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
        _write_json(path / "compose.resolved.json", config)
        _write_json(path / "compose.rollback.json", pinned)
        _write_json(path / "deployment.json", deployment)
        _write_json(path / "database.json", database)
        images = sorted({app["image_id"] for app in deployment["apps"].values()})
        run_command(["docker", "image", "save", "--output", str(path / "images.tar"), *images])
        _validate_archive(path / "images.tar", images)
        for service, original in containers.items():
            current = _container(compose, service)
            _require(current["Id"] == original["Id"] and current.get("State", {}).get("Running"),
                     "Deployment changed during capture; incomplete snapshot must not be used.")
        # Commit marker is written LAST. Existing source snapshots are never touched.
        _write_json(path / "manifest.json", {
            "kind": KIND, "format_version": FORMAT_VERSION, "deployment_snapshot": True,
            "created_utc": datetime.now(timezone.utc).isoformat(), "sentinel": sentinel,
            "files_sha256": {name: _sha256(path / name) for name in sorted(ARTIFACTS)},
        })
    finally:
        os.umask(old_umask)
    return path


def validate_snapshot(snapshot):
    """Pure local validation, returning parsed data. No subprocess or writes."""
    path = _private_location(snapshot)
    _require(path.is_dir(), "Snapshot directory does not exist.")
    _private_permissions(path)
    _require({p.name for p in path.iterdir()} == ARTIFACTS | {"manifest.json"},
             "Incomplete snapshot or unexpected files (source-only snapshots are unsupported).")
    for name in ARTIFACTS | {"manifest.json"}:
        item = path / name
        _require(not item.is_symlink() and stat.S_ISREG(item.lstat().st_mode)
                 and item.stat().st_nlink == 1, "Snapshot files must be regular, unlinked private files.")
        _private_permissions(item)
    manifest = _json((path / "manifest.json").read_text(encoding="utf-8"))
    _require(isinstance(manifest, dict) and manifest.get("kind") == KIND
             and manifest.get("format_version") == FORMAT_VERSION
             and manifest.get("deployment_snapshot") is True,
             "Not a deployed Docker snapshot; local-source-only cannot be rolled back.")
    hashes = manifest.get("files_sha256")
    _require(isinstance(hashes, dict) and set(hashes) == ARTIFACTS,
             "Manifest must hash exactly the required artifact paths.")
    for name, digest in hashes.items():
        _require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                 and _sha256(path / name) == digest, "Snapshot checksum mismatch.")
    data = {name: _json((path / (name + ".json")).read_text(encoding="utf-8"))
            for name in ("deployment", "database", "compose.resolved", "compose.rollback")}
    deployment = data["deployment"]
    _require(isinstance(deployment, dict) and isinstance(deployment.get("apps"), dict)
             and set(deployment["apps"]) == set(APP_SERVICES), "Invalid app deployment metadata.")
    for app in deployment["apps"].values():
        _require(isinstance(app, dict) and isinstance(app.get("image_id"), str)
                 and IMAGE_ID.fullmatch(app["image_id"]), "Invalid image ID in snapshot.")
    project = deployment.get("project_name")
    directory = deployment.get("project_directory")
    _require(isinstance(project, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project)
             and isinstance(directory, str) and Path(directory).is_absolute()
             and isinstance(deployment.get("daemon_id"), str) and deployment["daemon_id"],
             "Invalid project/host identity.")
    _require(isinstance(deployment.get("postgres_mounts"), list) and deployment["postgres_mounts"]
             and isinstance(deployment.get("database"), dict)
             and set(deployment["database"]) == {"user", "name"}
             and all(isinstance(v, str) and v for v in deployment["database"].values()),
             "Invalid Postgres identity.")
    _require(data["compose.resolved"].get("name") == project,
             "Compose project differs from deployment metadata.")
    _require(data["compose.rollback"] == _pinned_config(data["compose.resolved"], deployment),
             "Rollback config must exactly match resolved config pinned to captured images.")
    _validate_materials(data["database"].get("materials"))
    _require(isinstance(data["database"].get("schema_columns"), list), "Missing schema metadata.")
    build_restore_sql(data["database"]["materials"], manifest.get("sentinel"))
    _validate_archive(path / "images.tar", [a["image_id"] for a in deployment["apps"].values()])
    return {"path": path, "manifest": manifest, **data}


def rollback(snapshot):
    """Validate everything, load immutable images, stop apps, restore pointers, start apps."""
    data = validate_snapshot(snapshot)
    path, deployment = data["path"], data["deployment"]
    compose = _compose(path / "compose.rollback.json", deployment["project_name"],
                       deployment["project_directory"], isolated=True)
    _require(run_command(["docker", "info", "--format", "{{.ID}}"]).strip() == deployment["daemon_id"],
             "Rollback must target the original Docker daemon.")
    pg = _container(compose, "postgres")
    _identity(pg, deployment["project_name"], "postgres")
    _require(pg.get("State", {}).get("Running")
             and _pg_mounts(pg) == deployment["postgres_mounts"]
             and _pg_database(pg) == deployment["database"],
             "Existing Postgres container/database/volume does not match the snapshot.")
    # Compose parsing and read-only SQL checks happen before image load or app stop.
    parsed = _json(run_command(compose + ["config", "--format", "json"]))
    for service in APP_SERVICES:
        _require(parsed.get("services", {}).get(service, {}).get("image")
                 == deployment["apps"][service]["image_id"], "Compose did not preserve pinned image IDs.")
        container = _container(compose, service, allow_missing=True)
        if container is not None:
            _identity(container, deployment["project_name"], service)
    materials = data["database"]["materials"]
    sentinel = data["manifest"]["sentinel"]
    # Temp table writes are transaction-local; deployed tables are only read here.
    _psql(compose, deployment["database"], "BEGIN;\n" + _baseline_sql(materials) + f"""
DO $release$
BEGIN
  IF EXISTS (SELECT 1 FROM findings WHERE prompt_version = {_literal(sentinel)}) THEN
    RAISE EXCEPTION 'Rollback sentinel collision';
  END IF;
END $release$;
SELECT id, status, processed_prompt_version, processing_error FROM materials LIMIT 0;
ROLLBACK;
""")
    sql = build_restore_sql(materials, sentinel)
    # First persistent mutation. No builds, tags, pulls, DB recreation, or volume operations.
    run_command(["docker", "image", "load", "--input", str(path / "images.tar")])
    for ident in sorted({a["image_id"] for a in deployment["apps"].values()}):
        _require(_json(run_command(["docker", "image", "inspect", ident]))[0].get("Id") == ident,
                 "Loaded image ID differs from the captured image.")
    try:
        run_command(compose + ["stop", *APP_SERVICES])
        for service in APP_SERVICES:
            container = _container(compose, service, allow_missing=True)
            _require(container is None or not container.get("State", {}).get("Running"),
                     "App container is still running; database pointers were not restored.")
        _psql(compose, deployment["database"], sql)
        run_command(compose + ["up", "--detach", "--no-deps", "--no-build", "--pull", "never",
                               "--force-recreate", *APP_SERVICES])
        for service in APP_SERVICES:
            container = _container(compose, service)
            _require(container.get("State", {}).get("Running")
                     and container.get("Image") == deployment["apps"][service]["image_id"],
                     "An app did not start with its captured image.")
        current_pg = _container(compose, "postgres")
        _require(current_pg["Id"] == pg["Id"] and _pg_mounts(current_pg) == deployment["postgres_mounts"],
                 "Postgres changed during rollback.")
    except (ReleaseError, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        # Never restart the newer release on a partial failure. Keep a rerunnable snapshot.
        try:
            run_command(compose + ["stop", *APP_SERVICES])
        except ReleaseError:
            pass
        raise ReleaseError("Rollback interrupted. Apps may be stopped; fix the cause and rerun the same command. "
                           "No findings were deleted; no Postgres/volume operation was requested.") from None
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    grab = commands.add_parser("capture", help="Capture the running pre-release Docker deployment privately")
    grab.add_argument("snapshot", help="New directory outside every Git working tree; parent must exist")
    grab.add_argument("--compose-file", default=None)
    grab.add_argument("--project-name", default=None)
    restore = commands.add_parser("rollback", help="Restore one validated deployed snapshot")
    restore.add_argument("snapshot")
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            capture(args.snapshot, args.compose_file, args.project_name)
        else:
            rollback(args.snapshot)
    except ReleaseError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        # Deliberately suppress exception payloads/tracebacks: config/SQL can contain secrets.
        print("Release operation failed. Check private snapshot, deployment identity and Docker/psql availability. "
              "If rollback started, apps may be stopped; correct the cause and rerun. "
              "No automatic fallback to the newer release is attempted.", file=sys.stderr)
        return 1
    print("Private deployment snapshot captured." if args.command == "capture" else "Captured release restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
