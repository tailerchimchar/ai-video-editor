"""Storage backend tests: LocalStorage and protocol compliance."""

import pytest

from ai_video_editor.storage import LocalStorage, StorageBackend, StorageError


class TestLocalStorage:
    def test_implements_protocol(self):
        storage = LocalStorage()
        assert isinstance(storage, StorageBackend)

    def test_write_and_read_bytes(self, tmp_path):
        storage = LocalStorage()
        path = tmp_path / "test.txt"
        data = b"hello world"

        storage.write_bytes(str(path), data)
        assert storage.read_bytes(str(path)) == data

    def test_write_creates_parent_dirs(self, tmp_path):
        storage = LocalStorage()
        path = tmp_path / "a" / "b" / "c" / "test.txt"

        storage.write_bytes(str(path), b"data")
        assert path.exists()

    def test_exists_true_for_file(self, tmp_path):
        storage = LocalStorage()
        path = tmp_path / "file.txt"
        path.write_bytes(b"x")

        assert storage.exists(str(path))

    def test_exists_false_for_missing(self, tmp_path):
        storage = LocalStorage()
        assert not storage.exists(str(tmp_path / "missing"))

    def test_mkdir_creates_directory(self, tmp_path):
        storage = LocalStorage()
        path = tmp_path / "new_dir"

        storage.mkdir(str(path))
        assert path.is_dir()

    def test_mkdir_with_parents(self, tmp_path):
        storage = LocalStorage()
        path = tmp_path / "a" / "b" / "c"

        storage.mkdir(str(path), parents=True)
        assert path.is_dir()

    def test_listdir(self, tmp_path):
        storage = LocalStorage()
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        (tmp_path / "subdir").mkdir()

        items = storage.listdir(str(tmp_path))
        assert set(items) == {"a.txt", "b.txt", "subdir"}

    def test_remove_file(self, tmp_path):
        storage = LocalStorage()
        path = tmp_path / "to_delete.txt"
        path.write_bytes(b"data")

        storage.remove(str(path))
        assert not path.exists()

    def test_remove_missing_is_noop(self, tmp_path):
        storage = LocalStorage()
        storage.remove(str(tmp_path / "missing"))

    def test_rmtree(self, tmp_path):
        storage = LocalStorage()
        path = tmp_path / "tree"
        path.mkdir()
        (path / "file.txt").write_bytes(b"x")
        (path / "subdir").mkdir()
        (path / "subdir" / "nested.txt").write_bytes(b"y")

        storage.rmtree(str(path))
        assert not path.exists()

    def test_rmtree_missing_is_noop(self, tmp_path):
        storage = LocalStorage()
        storage.rmtree(str(tmp_path / "missing"))

    def test_copy_to_local(self, tmp_path):
        storage = LocalStorage()
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_bytes(b"content")

        storage.copy_to_local(str(src), str(dst))
        assert dst.read_bytes() == b"content"

    def test_copy_from_local(self, tmp_path):
        storage = LocalStorage()
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_bytes(b"content")

        storage.copy_from_local(str(src), str(dst))
        assert dst.read_bytes() == b"content"

    def test_read_missing_raises_storage_error(self, tmp_path):
        storage = LocalStorage()
        with pytest.raises(StorageError):
            storage.read_bytes(str(tmp_path / "missing"))

    def test_listdir_missing_raises_storage_error(self, tmp_path):
        storage = LocalStorage()
        with pytest.raises(StorageError):
            storage.listdir(str(tmp_path / "missing"))
