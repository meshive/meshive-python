import re

import meshive
from meshive import _version


# PEP 440 pre-releases (0.1.0rc1) are allowed too — an rc is tagged on dev, published to PyPI, and only installed with `pip install --pre`.
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?([.-].+)?$")


def test_version_is_semver():
    assert SEMVER_RE.match(meshive.__version__)


def test_version_aliases_match():
    assert meshive.version == meshive.__version__
    assert _version.__version__ == meshive.__version__


def test_installed_metadata_matches_source():
    from importlib.metadata import version as get_metadata_version

    assert get_metadata_version("meshive") == meshive.__version__
