"""T3: private release capture and rollback, with no Docker/DB access.

Production code was authored by the release agent. These tests use real files,
checksums and Docker-save tar structure; only the subprocess boundary is mocked.
"""
from copy import deepcopy
from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import release


SENTINEL = "__release_no_baseline_" + "a" * 32 + "__"
SECRET = "private-example-token"


def baseline(ident=1, pointer="v5", resolved="v5"):
    return dict(id=ident, url=f"https://example.org/{ident}", status="done",
                processing_error=None, processed_prompt_version=pointer,
                resolved_prompt_version=resolved)


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir(mode=0o700)
        self.config_bytes = b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
        self.image = "sha256:" + hashlib.sha256(self.config_bytes).hexdigest()
        self.mounts = [dict(Type="volume", Name="study_pgdata", Source="/volumes/pgdata",
                            Destination="/var/lib/postgresql/data", RW=True)]
        self.config = dict(name="study", services={
            "postgres": dict(image="postgres:17-alpine", environment={
                "POSTGRES_USER": "owner", "POSTGRES_DB": "study", "POSTGRES_PASSWORD": SECRET}),
            **{name: dict(image="app:previous", environment={"BLOCK": name, "KEY": "private$$token"})
               for name in release.APP_SERVICES},
        }, volumes={"pgdata": {"name": "study_pgdata"}})
        self.deployment = dict(daemon_id="daemon-1", project_name="study",
                               project_directory=str(self.project), postgres_mounts=self.mounts,
                               database=dict(user="owner", name="study"),
                               apps={name: dict(image_id=self.image) for name in release.APP_SERVICES})
        self.database = dict(materials=[baseline(), baseline(2, None, "legacy")], schema_columns=[])
        self.pinned = release._pinned_config(self.config, self.deployment)
        for name, value in {"compose.resolved": self.config, "compose.rollback": self.pinned,
                            "deployment": self.deployment, "database": self.database}.items():
            self.write_json(self.snapshot / (name + ".json"), value)
        self.write_archive(self.snapshot / "images.tar")
        self.rehash()
        # Windows user's home has a legacy .git entry. Fixture tests isolate
        # that path guard; separate tests below exercise it without this patch.
        self.location = patch.object(release, "_private_location", side_effect=lambda value: Path(value).resolve())
        self.location.start()
        self.addCleanup(self.location.stop)

    def write_json(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)

    def write_archive(self, path, unsafe=False):
        with tarfile.open(path, "w") as archive:
            entries = {"config.json": self.config_bytes,
                       "manifest.json": json.dumps([dict(Config="config.json", Layers=[], RepoTags=[])]).encode()}
            if unsafe:
                entries["../escape"] = b"unsafe"
            for name, raw in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        path.chmod(0o600)

    def rehash(self):
        manifest = dict(kind=release.KIND, format_version=release.FORMAT_VERSION,
                        deployment_snapshot=True, sentinel=SENTINEL,
                        files_sha256={name: hashlib.sha256((self.snapshot / name).read_bytes()).hexdigest()
                                      for name in release.ARTIFACTS})
        self.write_json(self.snapshot / "manifest.json", manifest)

    def container(self, service, running=True):
        env = self.config["services"][service]["environment"]
        return dict(Id=service + "-container", Image=self.image,
                    State={"Running": running}, Mounts=self.mounts if service == "postgres" else [],
                    Config=dict(Env=[key + "=" + value.replace("$$", "$") for key, value in env.items()],
                                Labels={"com.docker.compose.project": "study",
                                        "com.docker.compose.service": service,
                                        "com.docker.compose.config-hash": "hash-" + service}))


class SnapshotValidationTests(ReleaseFixture):
    def test_valid_snapshot_checks_real_hashes_and_archive_without_commands(self):
        with patch.object(release, "run_command") as command:
            data = release.validate_snapshot(self.snapshot)
        command.assert_not_called()
        self.assertEqual(data["database"]["materials"], self.database["materials"])
        self.assertEqual(data["compose.rollback"]["services"]["worker"]["image"], self.image)

    def test_corruption_is_rejected_before_any_docker_call(self):
        (self.snapshot / "database.json").write_text("{}", encoding="utf-8")
        with patch.object(release, "run_command") as command:
            with self.assertRaises(release.ReleaseError):
                release.rollback(self.snapshot)
        command.assert_not_called()

    def test_source_only_snapshot_is_not_accepted_as_deployment(self):
        manifest = json.loads((self.snapshot / "manifest.json").read_text())
        manifest.update(kind="local-source-only", deployment_snapshot=False)
        self.write_json(self.snapshot / "manifest.json", manifest)
        with patch.object(release, "run_command") as command:
            with self.assertRaises(release.ReleaseError):
                release.rollback(self.snapshot)
        command.assert_not_called()

    def test_missing_or_extra_files_are_rejected(self):
        (self.snapshot / "unexpected.txt").write_text("extra")
        with self.assertRaises(release.ReleaseError):
            release.validate_snapshot(self.snapshot)
        (self.snapshot / "unexpected.txt").unlink()
        (self.snapshot / "images.tar").unlink()
        with self.assertRaises(release.ReleaseError):
            release.validate_snapshot(self.snapshot)

    def test_tar_traversal_is_rejected_even_with_matching_artifact_hash(self):
        self.write_archive(self.snapshot / "images.tar", unsafe=True)
        self.rehash()
        with self.assertRaises(release.ReleaseError):
            release.validate_snapshot(self.snapshot)

    def test_changed_rollback_env_is_rejected_even_after_rehash(self):
        changed = deepcopy(self.pinned)
        changed["services"]["worker"]["environment"]["KEY"] = "different"
        self.write_json(self.snapshot / "compose.rollback.json", changed)
        self.rehash()
        with self.assertRaises(release.ReleaseError):
            release.validate_snapshot(self.snapshot)

    def test_source_capture_never_overwrites_existing_directory(self):
        with patch.object(release, "run_command") as command:
            with self.assertRaises(release.ReleaseError):
                release.capture(self.snapshot)
        command.assert_not_called()


class RestoreSQLTests(unittest.TestCase):
    def test_restores_resolved_legacy_pointer_and_quarantines_new_only_materials(self):
        rows = [baseline(), baseline(2, None, "legacy"), baseline(3, None, None)]
        sql = release.build_restore_sql(rows, SENTINEL)
        self.assertIn("COALESCE(b.resolved_prompt_version", sql)
        self.assertIn("WHEN b.resolved_prompt_version IS NULL THEN 'new'", sql)
        self.assertIn("WHERE NOT EXISTS (SELECT 1 FROM release_baseline b WHERE b.id = m.id)", sql)
        self.assertIn("LOCK TABLE materials, findings", sql)
        self.assertTrue(sql.startswith("BEGIN;"))
        self.assertTrue(sql.strip().endswith("COMMIT;"))
        self.assertNotIn("DELETE FROM", sql.upper())
        self.assertNotIn("TRUNCATE", sql.upper())
        self.assertNotIn("UPDATE findings", sql)

    def test_sql_quotes_source_urls_and_errors(self):
        row = baseline()
        row.update(url="https://example.org/o'reilly", processing_error="line\\one's error")
        sql = release.build_restore_sql([row], SENTINEL)
        self.assertIn("o''reilly", sql)
        self.assertIn("one''s", sql)

    def test_duplicate_ids_invalid_sentinel_and_conflicting_pointers_fail_closed(self):
        for rows, sentinel in (([baseline(), baseline()], SENTINEL),
                               ([baseline()], "unsafe'"),
                               ([baseline(pointer="v5", resolved="v7")], SENTINEL)):
            with self.subTest(rows=rows, sentinel=sentinel):
                with self.assertRaises(release.ReleaseError):
                    release.build_restore_sql(rows, sentinel)


class RollbackOperationTests(ReleaseFixture):
    def mock_runtime(self, fail_up=False, daemon="daemon-1", bad_mount=False):
        events, state = [], {"stopped": False, "restored": False}
        def command(args, input_text=None):
            events.append(("command", list(args)))
            if args[:2] == ["docker", "info"]:
                return daemon
            if args[:3] == ["docker", "image", "load"]:
                return "loaded"
            if args[:3] == ["docker", "image", "inspect"]:
                return json.dumps([{"Id": self.image}])
            if args[-3:] == ["config", "--format", "json"]:
                return json.dumps(self.pinned)
            if "stop" in args:
                state["stopped"] = True
                return ""
            if "up" in args:
                if fail_up:
                    raise release.ReleaseError("simulated start failure")
                state["restored"] = True
                state["stopped"] = False
                return ""
            raise AssertionError(args)
        def container(compose, service, allow_missing=False):
            result = self.container(service, service == "postgres" or not state["stopped"])
            if service == "postgres" and bad_mount:
                result["Mounts"] = [{**self.mounts[0], "Name": "wrong-volume"}]
            return result
        def psql(compose, database, sql):
            events.append(("sql", sql))
            return ""
        return events, state, command, container, psql

    def test_single_operation_restores_only_apps_with_pinned_images_and_sql(self):
        events, state, command, container, psql = self.mock_runtime()
        with patch.object(release, "run_command", side_effect=command), \
             patch.object(release, "_container", side_effect=container), \
             patch.object(release, "_psql", side_effect=psql):
            release.rollback(self.snapshot)
        commands = [entry for kind, entry in events if kind == "command"]
        stop = next(args for args in commands if "stop" in args)
        up = next(args for args in commands if "up" in args)
        self.assertEqual(stop[-3:], list(release.APP_SERVICES))
        self.assertEqual(up[-3:], list(release.APP_SERVICES))
        for flag in ("--no-deps", "--no-build", "--force-recreate"):
            self.assertIn(flag, up)
        self.assertIn("never", up)
        self.assertNotIn("down", [arg for args in commands for arg in args])
        sqls = [value for kind, value in events if kind == "sql"]
        self.assertEqual(len(sqls), 2)
        self.assertNotIn("UPDATE materials", sqls[0])
        self.assertIn("UPDATE materials", sqls[1])
        stop_index = events.index(("command", stop))
        restore_index = next(i for i, (kind, value) in enumerate(events) if kind == "sql" and "UPDATE materials" in value)
        self.assertLess(stop_index, restore_index)
        self.assertLess(restore_index, events.index(("command", up)))
        self.assertTrue(state["restored"])

    def test_wrong_daemon_or_volume_causes_no_image_load_stop_or_update(self):
        for options in ({"daemon": "wrong-daemon"}, {"bad_mount": True}):
            events, _, command, container, psql = self.mock_runtime(**options)
            with self.subTest(options=options), \
                 patch.object(release, "run_command", side_effect=command), \
                 patch.object(release, "_container", side_effect=container), \
                 patch.object(release, "_psql", side_effect=psql):
                with self.assertRaises(release.ReleaseError):
                    release.rollback(self.snapshot)
            commands = [args for kind, args in events if kind == "command"]
            self.assertFalse(any("load" in args or "stop" in args or "up" in args for args in commands))
            self.assertFalse(any(kind == "sql" and "UPDATE materials" in value for kind, value in events))

    def test_failed_start_stops_apps_and_leaves_snapshot_reusable(self):
        events, state, command, container, psql = self.mock_runtime(fail_up=True)
        with patch.object(release, "run_command", side_effect=command), \
             patch.object(release, "_container", side_effect=container), \
             patch.object(release, "_psql", side_effect=psql):
            with self.assertRaises(release.ReleaseError):
                release.rollback(self.snapshot)
        self.assertTrue(state["stopped"])
        self.assertEqual(sum(kind == "command" and "stop" in args for kind, args in events), 2)
        self.assertIsNotNone(release.validate_snapshot(self.snapshot))


class CaptureOperationTests(ReleaseFixture):
    def capture_runtime(self, fail_save=False):
        events = []
        def command(args, input_text=None):
            events.append(list(args))
            if args[-3:] == ["config", "--format", "json"]:
                return json.dumps(self.config)
            if "--hash" in args:
                return args[-1] + " hash-" + args[-1]
            if args[:2] == ["docker", "info"]:
                return "daemon-1"
            if args[:3] == ["docker", "image", "inspect"]:
                return json.dumps([dict(Id=self.image, RepoDigests=[], Config={"Labels": {}})])
            if args[:3] == ["docker", "image", "save"]:
                if fail_save:
                    raise release.ReleaseError("save failed")
                shutil.copyfile(self.snapshot / "images.tar", args[args.index("--output")+1])
                return ""
            raise AssertionError(args)
        def psql(compose, database, sql):
            return "0" if "SELECT count(*) FROM findings" in sql else json.dumps(self.database)
        return events, command, psql

    def test_capture_saves_real_private_artifacts_with_no_service_mutations(self):
        target = self.root / "new-capture"
        compose_file = self.project / "compose.json"
        self.write_json(compose_file, self.config)
        events, command, psql = self.capture_runtime()
        with patch.object(release, "run_command", side_effect=command), \
             patch.object(release, "_container", side_effect=lambda c, s: self.container(s)), \
             patch.object(release, "_psql", side_effect=psql):
            release.capture(target, compose_file, "study")
        saved = release.validate_snapshot(target)
        self.assertEqual(saved["database"]["materials"], self.database["materials"])
        self.assertEqual(saved["compose.resolved"]["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"], SECRET)
        self.assertFalse(any(arg in {"stop", "up", "down", "load"} for args in events for arg in args))
        self.assertEqual(set(p.name for p in target.iterdir()), release.ARTIFACTS | {"manifest.json"})

    def test_interrupted_capture_has_no_commit_marker_and_is_not_overwritten(self):
        target = self.root / "interrupted"
        compose_file = self.project / "compose.json"
        self.write_json(compose_file, self.config)
        _, command, psql = self.capture_runtime(fail_save=True)
        with patch.object(release, "run_command", side_effect=command), \
             patch.object(release, "_container", side_effect=lambda c, s: self.container(s)), \
             patch.object(release, "_psql", side_effect=psql):
            with self.assertRaises(release.ReleaseError):
                release.capture(target, compose_file, "study")
        self.assertTrue(target.is_dir())
        self.assertFalse((target / "manifest.json").exists())
        with patch.object(release, "run_command") as invoke:
            with self.assertRaises(release.ReleaseError):
                release.capture(target, compose_file, "study")
        invoke.assert_not_called()


class ReleaseBoundaryTests(unittest.TestCase):
    def test_repository_paths_are_rejected(self):
        with self.assertRaises(release.ReleaseError):
            release._private_location(release.REPO_ROOT / "private-snapshot")

    def test_command_failure_does_not_echo_secret_stdout_or_stderr(self):
        result = subprocess.CompletedProcess([], 1, stdout=SECRET, stderr=SECRET)
        with patch.object(release.subprocess, "run", return_value=result):
            with self.assertRaises(release.ReleaseError) as caught:
                release.run_command(["docker", "info"])
        self.assertNotIn(SECRET, str(caught.exception))

    def test_cli_suppresses_secret_exception_payload(self):
        errors = io.StringIO()
        with patch.object(release, "rollback", side_effect=KeyError(SECRET)), redirect_stderr(errors):
            code = release.main(["rollback", "unused"])
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, errors.getvalue())


if __name__ == "__main__":
    unittest.main()
