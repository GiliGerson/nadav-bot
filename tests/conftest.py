import numpy as np
import pandas as pd
import pytest


def make_history(closes, volumes=None) -> pd.DataFrame:
    index = pd.bdate_range("2025-01-01", periods=len(closes))
    volumes = volumes if volumes is not None else [1_000_000] * len(closes)
    return pd.DataFrame({"Close": closes, "Volume": volumes}, index=index)


@pytest.fixture
def uptrend():
    return make_history(list(np.linspace(100, 200, 252)))


@pytest.fixture
def make():
    return make_history
