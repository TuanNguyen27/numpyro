import os

import pytest

from numpyro.util import set_rng_seed

from jax.config import config; config.update('jax_platform_name', 'cpu')  # noqa: E702


def pytest_runtest_setup(item):
    if 'JAX_ENABLE_x64' in os.environ:
        config.update('jax_enable_x64', True)
    set_rng_seed(0)


def pytest_runtest_call(item):
    try:
        item.runtest()
    except NotImplementedError as e:
        pytest.xfail(str(e))
