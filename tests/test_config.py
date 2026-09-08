from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from nextgensquash.config import Config


def test_from_dict_reads_every_key():
    config = Config.from_dict(
        {
            "IGNORED_APPS": ["sessions"],
            "FROZEN_APPS": ["queue"],
            "EARLY_MODELS": {"core": ["instancesetting"]},
            "STUB_CLAIMS": {"core": [["core", "0001_initial"]]},
            "OPERATIONS_MODULE": "core.squash_operations",
        }
    )

    assert config.ignored_apps == frozenset({"sessions"})
    assert config.frozen_apps == frozenset({"queue"})
    assert config.early_models == {"core": frozenset({"instancesetting"})}
    assert config.stub_claims == {"core": (("core", "0001_initial"),)}
    assert config.operations_module == "core.squash_operations"


@pytest.mark.parametrize("raw", [{}, {"IGNORED_APPS": None, "STUB_CLAIMS": None}])
def test_from_dict_defaults_to_empty(raw):
    assert Config.from_dict(raw) == Config()


def test_from_dict_rejects_an_unknown_key():
    with pytest.raises(ImproperlyConfigured, match="EARLY_MODEL"):
        Config.from_dict({"EARLY_MODEL": {}})
