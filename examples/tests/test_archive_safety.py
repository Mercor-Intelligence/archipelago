import importlib.util
import io
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_example_module(
    relative_path: str, module_name: str, stubs: dict[str, types.ModuleType]
):
    old_modules = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            module_name, REPO_ROOT / relative_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load module spec for {relative_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old_module in old_modules.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


def load_archive_safety():
    return load_example_module(
        "examples/archive_safety.py",
        "archive_safety",
        {},
    )


def load_simple_example():
    requests = types.ModuleType("requests")
    requests.RequestException = Exception

    return load_example_module(
        "examples/simple_task/main.py",
        "simple_task_main",
        {"requests": requests},
    )


class ArchiveSafetyTests(unittest.TestCase):
    def test_hugging_face_member_validation_rejects_unsafe_paths(self):
        module = load_archive_safety()

        unsafe_names = [
            "../evil.txt",
            "/tmp/evil.txt",
            "C:/evil.txt",
            "..\\evil.txt",
            "a//b.txt",
            "a///",
            "./b.txt",
            "a/../b.txt",
        ]

        for name in unsafe_names:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    module.safe_archive_member_path(name)

    def test_hugging_face_safe_extract_rejects_traversal_member(self):
        module = load_archive_safety()

        with tempfile.TemporaryDirectory() as tmp:
            outside_path = Path(tmp).parent / f"{Path(tmp).name}_evil.txt"
            outside_path.unlink(missing_ok=True)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("filesystem/ok.txt", "ok")
                zf.writestr(f"../{outside_path.name}", "bad")
            archive.seek(0)

            with zipfile.ZipFile(archive) as zf:
                with self.assertRaises(ValueError):
                    module.safe_extract_zip(zf, Path(tmp))

            self.assertFalse(outside_path.exists())

    def test_simple_zip_to_tar_rejects_traversal_after_prefix_strip(self):
        module = load_simple_example()

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "snapshot.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("filesystem/../evil.txt", "bad")

            with self.assertRaises(ValueError):
                module.zip_to_tar_gz(zip_path)

    def test_simple_zip_to_tar_preserves_valid_relative_paths(self):
        module = load_simple_example()

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = Path(tmp) / "snapshot.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("filesystem/animals/gorilla.png", b"png")

            tar_path = module.zip_to_tar_gz(zip_path)
            with tarfile.open(tar_path, "r:gz") as tar:
                self.assertIn("animals/gorilla.png", tar.getnames())


if __name__ == "__main__":
    unittest.main()
