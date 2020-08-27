# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from flax import nn as flax_nn

def haiku_module(name, nn, input_shape=None):
    """
    Declare a :mod:`~haiku` style neural network inside a
    model so that its parameters are registered for optimization via
    :func:`~numpyro.primitives.param` statements.

    :param str name: name of the module to be registered.
    :param haiku.Module nn: a haiku Module which has .init and .apply methods
    :param tuple input_shape: shape of the input taken by the
        neural network.
    :return: a `apply_fn` with bound parameters that takes an array
        as an input and returns the neural network transformed output
        array.
    """
    module_key = name + '$params'
    nn_params = numpyro.param(module_key)
    if nn_params is None:
        if input_shape is None:
            raise ValueError('Valid value for `input_size` needed to initialize.')
        # feed in dummy data to init params
        rng_key = numpyro.sample(name + '$rng_key', PRNGIdentity())
        nn_params = nn.init(rng_key, jnp.ones(input_shape))
        numpyro.param(module_key, nn_params)
    return lambda x: nn.apply(nn_params, rng_key, x)


def flax_module(name, nn, input_shape=None):
    """
    Declare a :mod:`~flax` style neural network inside a
    model so that its parameters are registered for optimization via
    :func:`~numpyro.primitives.param` statements.

    :param str name: name of the module to be registered.
    :param flax.nn.Module nn: a `flax` Module which has .init and .apply methods
    :param tuple input_shape: shape of the input taken by the
        neural network.
    :return: a `apply_fn` with bound parameters that takes an array
        as an input and returns the neural network transformed output
        array.
    """
    module_key = name + '$params'
    nn_params = numpyro.param(module_key)
    if nn_params is None:
        if input_shape is None:
            raise ValueError('Valid value for `input_size` needed to initialize.')
        # feed in dummy data to init params
        rng_key = numpyro.sample(name + '$rng_key', PRNGIdentity())
        _, nn_params = nn.init(rng_key, jnp.ones(input_shape))
        numpyro.param(module_key, nn_params)
    return flax_nn.Model(model, nn_params)
