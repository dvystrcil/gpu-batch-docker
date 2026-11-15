Frozen copy of the four files bin/flathub-bump.py edits, as they were at
213c932 (OrcaSlicer 2.5.0, PrusaSlicer 2.9.7, GNOME 50 runtime 4337a389...).

tests/test_flathub_bump.py runs its scenarios against this copy, not the live
files: the live files are what a bump changes, so a test that assumed their
versions failed on the very first real bump (PR #28). Never update this copy
to follow a bump. test_the_live_files_are_up_to_date_with_their_own_pins
covers the live files' format.
