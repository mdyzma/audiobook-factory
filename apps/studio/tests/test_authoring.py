"""Writing: uploads, role corrections and the cast.

Everything here changes the project on disk, so the refusals matter as much as
the successes.
"""

from __future__ import annotations

import io

import pytest

from studio import authoring
from studio.authoring import AuthoringError


class TestFilenames:
    @pytest.mark.parametrize("given,expected", [
        ("Sołaris Test.txt", "solaris-test"),
        ("Łódź.epub", "lodz"),
        ("My Book (draft).pdf", "my-book-draft"),
        ("ŻÓŁĆ.txt", "zolc"),
    ])
    def test_transliterates_rather_than_dropping_letters(self, given, expected):
        # Rolling a separate slugify here once turned "Sołaris" into "soaris".
        assert authoring._slugify_filename(given) == expected

    def test_a_path_is_reduced_to_its_stem(self):
        assert authoring._slugify_filename("../../etc/passwd.txt") == "passwd"


class TestUploads:
    def test_stores_a_voice_sample(self, project):
        up = authoring.store_upload(project, "voice", "sample.MP3", io.BytesIO(b"x" * 32))
        assert up.path == project / "data" / "raw" / "voices" / "sample.mp3"
        assert up.bytes_written == 32

    def test_stores_an_ebook(self, project):
        up = authoring.store_upload(project, "book", "Solaris.epub", io.BytesIO(b"x" * 16))
        assert up.path.parent == project / "data" / "raw" / "books"

    def test_a_traversing_filename_cannot_escape(self, project):
        up = authoring.store_upload(project, "book", "../../../justfile.txt",
                                    io.BytesIO(b"x"))
        # Only the stem survives, and the directory is chosen here.
        assert up.path.parent == project / "data" / "raw" / "books"
        assert up.path.name == "justfile.txt"

    @pytest.mark.parametrize("name", ["evil.sh", "payload.exe", "noextension", "a.py"])
    def test_refuses_unexpected_extensions(self, project, name):
        with pytest.raises(AuthoringError, match="must be one of"):
            authoring.store_upload(project, "book", name, io.BytesIO(b"x"))

    def test_refuses_an_unknown_kind(self, project):
        with pytest.raises(AuthoringError, match="unknown upload kind"):
            authoring.store_upload(project, "system", "a.txt", io.BytesIO(b"x"))

    def test_refuses_an_empty_upload(self, project):
        with pytest.raises(AuthoringError, match="empty"):
            authoring.store_upload(project, "book", "a.txt", io.BytesIO(b""))

    def test_refuses_something_absurdly_large(self, project, monkeypatch):
        monkeypatch.setattr(authoring, "MAX_UPLOAD_BYTES", 64)
        with pytest.raises(AuthoringError, match="exceeds"):
            authoring.store_upload(project, "book", "a.txt", io.BytesIO(b"x" * 256))

    def test_an_oversized_upload_leaves_nothing_behind(self, project, monkeypatch):
        monkeypatch.setattr(authoring, "MAX_UPLOAD_BYTES", 64)
        with pytest.raises(AuthoringError):
            authoring.store_upload(project, "book", "big.txt", io.BytesIO(b"x" * 256))
        assert not (project / "data" / "raw" / "books" / "big.txt").exists()

    def test_a_failed_re_upload_does_not_destroy_the_existing_file(self, project, monkeypatch):
        # The upload used to open the target directly, truncating it, then
        # delete it on failure. Re-uploading a book you already had, over the
        # limit or from a dropped connection, destroyed the copy you had.
        authoring.store_upload(project, "book", "solaris.txt", io.BytesIO(b"oryginal"))
        kept = project / "data" / "raw" / "books" / "solaris.txt"

        monkeypatch.setattr(authoring, "MAX_UPLOAD_BYTES", 4)
        with pytest.raises(AuthoringError, match="exceeds"):
            authoring.store_upload(project, "book", "solaris.txt", io.BytesIO(b"x" * 256))

        assert kept.read_bytes() == b"oryginal"

    def test_an_empty_re_upload_does_not_destroy_the_existing_file(self, project):
        authoring.store_upload(project, "book", "solaris.txt", io.BytesIO(b"oryginal"))
        kept = project / "data" / "raw" / "books" / "solaris.txt"

        with pytest.raises(AuthoringError, match="empty"):
            authoring.store_upload(project, "book", "solaris.txt", io.BytesIO(b""))

        assert kept.read_bytes() == b"oryginal"

    def test_a_stream_that_dies_mid_upload_leaves_the_original_intact(self, project):
        authoring.store_upload(project, "book", "solaris.txt", io.BytesIO(b"oryginal"))
        kept = project / "data" / "raw" / "books" / "solaris.txt"

        class Dying:
            def __init__(self):
                self.calls = 0

            def read(self, _n):
                self.calls += 1
                if self.calls > 1:
                    raise ConnectionError("client went away")
                return b"y" * 16

        with pytest.raises(ConnectionError):
            authoring.store_upload(project, "book", "solaris.txt", Dying())

        assert kept.read_bytes() == b"oryginal"

    def test_a_successful_re_upload_replaces_the_file(self, project):
        authoring.store_upload(project, "book", "solaris.txt", io.BytesIO(b"stary"))
        authoring.store_upload(project, "book", "solaris.txt", io.BytesIO(b"nowy"))
        assert (project / "data" / "raw" / "books" / "solaris.txt").read_bytes() == b"nowy"

    def test_failed_uploads_leave_no_partial_files_behind(self, project, monkeypatch):
        monkeypatch.setattr(authoring, "MAX_UPLOAD_BYTES", 4)
        with pytest.raises(AuthoringError):
            authoring.store_upload(project, "book", "big.txt", io.BytesIO(b"x" * 256))
        assert list((project / "data" / "raw" / "books").iterdir()) == []

    def test_lists_what_has_been_uploaded(self, project):
        authoring.store_upload(project, "book", "a.txt", io.BytesIO(b"xy"))
        listed = authoring.list_raw(project, "book")
        assert [d["name"] for d in listed] == ["a.txt"]
        assert listed[0]["bytes"] == 2


class TestRoleCorrections:
    def test_sets_and_reads_a_correction(self, project):
        authoring.set_role(project, "solaris", "c1.xhtml#p3", "kelvin")
        assert authoring.get_roles(project, "solaris") == {"c1.xhtml#p3": "kelvin"}

    def test_an_empty_role_clears_the_correction(self, project):
        authoring.set_role(project, "solaris", "c1.xhtml#p3", "kelvin")
        authoring.set_role(project, "solaris", "c1.xhtml#p3", "")
        assert authoring.get_roles(project, "solaris") == {}

    @pytest.mark.parametrize("role", ["Kelvin Kelvin", "../etc", "a/b", "x" * 100, "ł"])
    def test_refuses_a_role_that_is_not_a_plain_name(self, project, role):
        with pytest.raises(AuthoringError, match="lowercase"):
            authoring.set_role(project, "solaris", "c1.xhtml#p3", role)

    def test_refuses_an_unknown_book(self, project):
        with pytest.raises(AuthoringError, match="no book"):
            authoring.set_role(project, "absent", "c1.xhtml#p3", "kelvin")

    def test_refuses_a_missing_source_ref(self, project):
        with pytest.raises(AuthoringError, match="source_ref"):
            authoring.set_role(project, "solaris", "", "kelvin")

    def test_corrections_are_keyed_by_paragraph_not_fragment(self, project):
        # This is the property that makes them survive re-chunking: chunk ids
        # encode position and are renumbered, source_ref names the paragraph.
        authoring.set_role(project, "solaris", "c1.xhtml#p3", "kelvin")
        stored = authoring.get_roles(project, "solaris")
        assert list(stored) == ["c1.xhtml#p3"]
        assert not any(key.startswith("ch0") for key in stored)


class TestCast:
    def _cast(self, project, body: str):
        (project / "config").mkdir(exist_ok=True)
        (project / "config" / "cast.yml").write_text(body, encoding="utf-8")

    def test_reads_a_cast(self, project):
        self._cast(project, "roles:\n  narrator:\n    voice: michal\n    speed: 1.0\n")
        assert authoring.read_cast(project)["narrator"]["voice"] == "michal"

    def test_writes_a_cast(self, project):
        authoring.write_cast(project, {
            "narrator": {"voice": "michal", "speed": 1.0},
            "kelvin": {"voice": "kelvin", "speed": 0.98},
        })
        cast = authoring.read_cast(project)
        assert cast["kelvin"]["voice"] == "kelvin"
        assert cast["kelvin"]["speed"] == 0.98

    def test_a_written_cast_keeps_its_explanatory_comments(self, project):
        authoring.write_cast(project, {"narrator": {"voice": "michal"}})
        text = (project / "config" / "cast.yml").read_text(encoding="utf-8")
        assert text.lstrip().startswith("#")

    def test_a_cast_must_have_a_narrator(self, project):
        with pytest.raises(AuthoringError, match="narrator"):
            authoring.write_cast(project, {"kelvin": {"voice": "kelvin"}})

    def test_a_role_needs_a_voice(self, project):
        with pytest.raises(AuthoringError, match="needs a voice"):
            authoring.write_cast(project, {"narrator": {"speed": 1.0}})

    @pytest.mark.parametrize("voice", ["../etc", "a/b", "a;b"])
    def test_refuses_an_unsafe_voice(self, project, voice):
        with pytest.raises(AuthoringError, match="invalid voice"):
            authoring.write_cast(project, {"narrator": {"voice": voice}})

    def test_refuses_an_unsafe_role_name(self, project):
        with pytest.raises(AuthoringError, match="invalid role"):
            authoring.write_cast(project, {"narrator": {"voice": "michal"},
                                           "../evil": {"voice": "michal"}})

    @pytest.mark.parametrize("speed", [0.1, 5.0, "fast"])
    def test_refuses_an_implausible_speed(self, project, speed):
        with pytest.raises(AuthoringError):
            authoring.write_cast(project, {"narrator": {"voice": "michal", "speed": speed}})

    def test_flags_a_voice_that_has_not_been_cloned(self, project):
        authoring.write_cast(project, {"narrator": {"voice": "nobody"}})
        text = (project / "config" / "cast.yml").read_text(encoding="utf-8")
        assert "no data/voices/nobody.json yet" in text

    def test_keeps_one_backup(self, project):
        self._cast(project, "roles:\n  narrator:\n    voice: old\n")
        authoring.backup_cast(project)
        authoring.write_cast(project, {"narrator": {"voice": "new"}})
        backup = project / "config" / "cast.yml.bak"
        assert backup.exists() and "old" in backup.read_text(encoding="utf-8")
