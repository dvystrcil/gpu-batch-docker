Frozen copy of the four files bin/watch-blender.py edits, as they were at
e32e0e2 (Blender 5.2.2, sha256 84098912...).

tests/test_watch_blender.py runs its scenarios against this copy, not the live
files: the live files are what a bump changes, so a test that assumed their
versions would fail on the first real bump (the same lesson as PR #28 and
tests/fixtures/flathub/repo-orca-2.5.0). Never update this copy to follow a
bump. test_the_live_files_parse covers the live files' format.

The other files in tests/fixtures/blender/ are real blender.org responses
captured 2026-11-18: /release/ (index), /release/Blender5.2/ (5.2.0 to 5.2.4),
/release/Blender5.3/ (5.3.0), and /release/Blender5.2/blender-5.2.4.sha256.
