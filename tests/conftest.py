import pytest
from build_db import build

from text2sql.db import SQLiteDatabase


@pytest.fixture(scope="session")
def retail_path(tmp_path_factory):
    return build(tmp_path_factory.mktemp("bench") / "retail.sqlite")


@pytest.fixture
def db(retail_path):
    return SQLiteDatabase(retail_path, timeout_s=1.0)
