"""Names a host imports from this package are public, and
the underscore names they had until 2.6.1 still import, as the same
objects. A patch or minor release never drops either (CONTRIBUTING.md,
Versioning): a host built against 2.6.1 keeps working."""
import importlib

import pytest

# (module, public name, the old underscore name)
PUBLIC = [
    ('atem_control.uploader', 'OwnershipLost', '_OwnershipLost'),
    ('atem_control.uploader', 'StateNotReady', '_StateNotReady'),
    ('atem_control.uploader', 'media_player_source_ids', '_media_player_source_ids'),
    ('atem_control.uploader', 'is_slot_safe', '_is_slot_safe'),
    ('atem_control.uploader', 'apply_macro_xml', '_apply_macro_xml'),
    ('atem_control.uploader', 'close_protocol', '_close_protocol'),
    ('atem_control.profile.views', 'build_apply_options', '_build_apply_options'),
]


@pytest.mark.parametrize('module, name, old', PUBLIC, ids=[f'{m}.{n}' for m, n, _ in PUBLIC])
def test_public_name_and_its_old_alias(module, name, old):
    mod = importlib.import_module(module)
    assert getattr(mod, old) is getattr(mod, name)


def test_media_player_source_ids_is_the_bmd_numbering():
    assert importlib.import_module('atem_control.uploader').media_player_source_ids(1) == {3020, 3021}
