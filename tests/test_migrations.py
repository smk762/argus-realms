"""Migrations are forward-only: an applied one is never edited, a change is a new file (`db.migrate` skips any name
already in `schema_migrations`, so an edit would never reach a database that ran the old text). Every migration is
pinned here by its sha256; add a new file's hash when you add the file, never change an existing one."""

import hashlib

from atlas_realms.db import migration_files

PINNED = {
    "001_init.sql": "7baa5fb49c3dc1e3ae1371eedfabf427130b55ea1c5b840d3e59c6a26e5818fb",
    "002_tick.sql": "7e4f2bd17211c55fe340d40353b9715dac6e38f3bcf6a759414d97536d56262a",
    "003_classes.sql": "85f274158aa902109b245964cf336bed3238837077ccab119a2aacb33dcc0d87",
    "004_veterans.sql": "018f5369d7eaca23a3c45886500599e81a530db8fc5400d1f132b763f0a379cf",
    "005_city_names.sql": "54ca4e890161062d3821f231261a78c75fb2e62fe92c44dfc799bf0491a30bee",
    "006_shields.sql": "21e46e5392e72cf68f9d14043dda6b3f9a1e526b0b9781ea971857d89ed79d93",
    "007_shield_toggle.sql": "689f43beef80f49f0215cce7423f82487a92c8be4a285ee3732142b2eb601cd8",
    "008_shared_shields_and_revenge.sql": "e329d1a5fa58fb69cc0e776ee469dca98ea0cc0998524f1b9de886f4ac83b092",
    "009_nemesis.sql": "5102e8d5ab9782e6b3debb5337b5b84cf60165df6c5af2230e83a74e5a85131d",
    "010_contracts.sql": "830abf18484dba58fa90f8cf8638129913ddb019cec777701f64f13c546e443a",
    "011_beacons.sql": "afcaf9572548e1559f8b639fdb598ce13fede13fcdb121876196e40fd7854890",
}


def test_no_applied_migration_is_edited_or_removed():
    have = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in migration_files()}
    assert {n: have.get(n) for n in PINNED} == PINNED, "an applied migration changed: add a new file instead"
    assert sorted(have) == sorted(PINNED), "pin the new migration's sha256 in PINNED"
