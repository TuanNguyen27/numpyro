from abc import ABC, abstractmethod
from collections import namedtuple
import functools
import math
from operator import attrgetter, itemgetter
import os
import warnings

import tqdm

from jax import jit, lax, partial, pmap, random, vmap
from jax.flatten_util import ravel_pytree
from jax.lib import xla_bridge
import jax.numpy as np
from jax.random import PRNGKey
from jax.tree_util import tree_flatten, tree_map, tree_multimap

from numpyro.diagnostics import summary
from numpyro.hmc_util import (
    IntegratorState,
    build_tree,
    euclidean_kinetic_energy,
    find_reasonable_step_size,
    initialize_model,
    velocity_verlet,
    warmup_adapter
)
from numpyro.util import cond, copy_docs_from, fori_collect, fori_loop, identity

HMCState = namedtuple('HMCState', ['i', 'z', 'z_grad', 'potential_energy', 'energy', 'num_steps', 'accept_prob',
                                   'mean_accept_prob', 'diverging', 'adapt_state', 'rng'])
"""
A :func:`~collections.namedtuple` consisting of the following fields:

 - **i** - iteration. This is reset to 0 after warmup.
 - **z** - Python collection representing values (unconstrained samples from
   the posterior) at latent sites.
 - **z_grad** - Gradient of potential energy w.r.t. latent sample sites.
 - **potential_energy** - Potential energy computed at the given value of ``z``.
 - **energy** - Sum of potential energy and kinetic energy of the current state.
 - **num_steps** - Number of steps in the Hamiltonian trajectory (for diagnostics).
 - **accept_prob** - Acceptance probability of the proposal. Note that ``z``
   does not correspond to the proposal if it is rejected.
 - **mean_accept_prob** - Mean acceptance probability until current iteration
   during warmup adaptation or sampling (for diagnostics).
 - **diverging** - A boolean value to indicate whether the current trajectory is diverging.
 - **adapt_state** - A ``AdaptState`` namedtuple which contains adaptation information
   during warmup:

   + **step_size** - Step size to be used by the integrator in the next iteration.
   + **inverse_mass_matrix** - The inverse mass matrix to be used for the next
     iteration.
   + **mass_matrix_sqrt** - The square root of mass matrix to be used for the next
     iteration. In case of dense mass, this is the Cholesky factorization of the
     mass matrix.

 - **rng** - random number generator seed used for the iteration.
"""


def _get_num_steps(step_size, trajectory_length):
    num_steps = np.clip(trajectory_length / step_size, a_min=1)
    # NB: casting to np.int64 does not take effect (returns np.int32 instead)
    # if jax_enable_x64 is False
    return num_steps.astype(xla_bridge.canonicalize_dtype(np.int64))


def _sample_momentum(unpack_fn, mass_matrix_sqrt, rng):
    eps = random.normal(rng, np.shape(mass_matrix_sqrt)[:1])
    if mass_matrix_sqrt.ndim == 1:
        r = np.multiply(mass_matrix_sqrt, eps)
        return unpack_fn(r)
    elif mass_matrix_sqrt.ndim == 2:
        r = np.dot(mass_matrix_sqrt, eps)
        return unpack_fn(r)
    else:
        raise ValueError("Mass matrix has incorrect number of dims.")


def get_diagnostics_str(hmc_state):
    return '{} steps of size {:.2e}. acc. prob={:.2f}'.format(hmc_state.num_steps,
                                                              hmc_state.adapt_state.step_size,
                                                              hmc_state.mean_accept_prob)


def get_progbar_desc_str(num_warmup, i):
    if i < num_warmup:
        return 'warmup'
    return 'sample'


def hmc(potential_fn, kinetic_fn=None, algo='NUTS'):
    r"""
    Hamiltonian Monte Carlo inference, using either fixed number of
    steps or the No U-Turn Sampler (NUTS) with adaptive path length.

    **References:**

    1. *MCMC Using Hamiltonian Dynamics*,
       Radford M. Neal
    2. *The No-U-turn sampler: adaptively setting path lengths in Hamiltonian Monte Carlo*,
       Matthew D. Hoffman, and Andrew Gelman.
    3. *A Conceptual Introduction to Hamiltonian Monte Carlo`*,
       Michael Betancourt

    :param potential_fn: Python callable that computes the potential energy
        given input parameters. The input parameters to `potential_fn` can be
        any python collection type, provided that `init_params` argument to
        `init_kernel` has the same type.
    :param kinetic_fn: Python callable that returns the kinetic energy given
        inverse mass matrix and momentum. If not provided, the default is
        euclidean kinetic energy.
    :param str algo: Whether to run ``HMC`` with fixed number of steps or ``NUTS``
        with adaptive path length. Default is ``NUTS``.
    :return: a tuple of callables (`init_kernel`, `sample_kernel`), the first
        one to initialize the sampler, and the second one to generate samples
        given an existing one.

    .. warning::
        Instead of using this interface directly, we would highly recommend you
        to use the higher level :class:`numpyro.infer.MCMC` API instead.

    **Example**

    .. testsetup::

        import jax
        from jax import random
        import jax.numpy as np
        import numpyro
        import numpyro.distributions as dist
        from numpyro.hmc_util import initialize_model
        from numpyro.infer.mcmc import hmc
        from numpyro.util import fori_collect

    .. doctest::

        >>> true_coefs = np.array([1., 2., 3.])
        >>> data = random.normal(random.PRNGKey(2), (2000, 3))
        >>> dim = 3
        >>> labels = dist.Bernoulli(logits=(true_coefs * data).sum(-1)).sample(random.PRNGKey(3))
        >>>
        >>> def model(data, labels):
        ...     coefs_mean = np.zeros(dim)
        ...     coefs = numpyro.sample('beta', dist.Normal(coefs_mean, np.ones(3)))
        ...     intercept = numpyro.sample('intercept', dist.Normal(0., 10.))
        ...     return numpyro.sample('y', dist.Bernoulli(logits=(coefs * data + intercept).sum(-1)), obs=labels)
        >>>
        >>> init_params, potential_fn, constrain_fn = initialize_model(random.PRNGKey(0),
        ...                                                            model, data, labels)
        >>> init_kernel, sample_kernel = hmc(potential_fn, algo='NUTS')
        >>> hmc_state = init_kernel(init_params,
        ...                         trajectory_length=10,
        ...                         num_warmup=300)
        >>> samples = fori_collect(0, 500, sample_kernel, hmc_state,
        ...                        transform=lambda state: constrain_fn(state.z))
        >>> print(np.mean(samples['beta'], axis=0))  # doctest: +SKIP
        [0.9153987 2.0754058 2.9621222]
    """
    if kinetic_fn is None:
        kinetic_fn = euclidean_kinetic_energy
    vv_init, vv_update = velocity_verlet(potential_fn, kinetic_fn)
    trajectory_len = None
    max_treedepth = None
    momentum_generator = None
    wa_update = None
    wa_steps = None
    max_delta_energy = 1000.
    if algo not in {'HMC', 'NUTS'}:
        raise ValueError('`algo` must be one of `HMC` or `NUTS`.')

    def init_kernel(init_params,
                    num_warmup,
                    step_size=1.0,
                    adapt_step_size=True,
                    adapt_mass_matrix=True,
                    dense_mass=False,
                    target_accept_prob=0.8,
                    trajectory_length=2*math.pi,
                    max_tree_depth=10,
                    run_warmup=True,
                    progbar=True,
                    rng=PRNGKey(0)):
        """
        Initializes the HMC sampler.

        :param init_params: Initial parameters to begin sampling. The type must
            be consistent with the input type to `potential_fn`.
        :param int num_warmup: Number of warmup steps; samples generated
            during warmup are discarded.
        :param float step_size: Determines the size of a single step taken by the
            verlet integrator while computing the trajectory using Hamiltonian
            dynamics. If not specified, it will be set to 1.
        :param bool adapt_step_size: A flag to decide if we want to adapt step_size
            during warm-up phase using Dual Averaging scheme.
        :param bool adapt_mass_matrix: A flag to decide if we want to adapt mass
            matrix during warm-up phase using Welford scheme.
        :param bool dense_mass: A flag to decide if mass matrix is dense or
            diagonal (default when ``dense_mass=False``)
        :param float target_accept_prob: Target acceptance probability for step size
            adaptation using Dual Averaging. Increasing this value will lead to a smaller
            step size, hence the sampling will be slower but more robust. Default to 0.8.
        :param float trajectory_length: Length of a MCMC trajectory for HMC. Default
            value is :math:`2\\pi`.
        :param int max_tree_depth: Max depth of the binary tree created during the doubling
            scheme of NUTS sampler. Defaults to 10.
        :param bool run_warmup: Flag to decide whether warmup is run. If ``True``,
            `init_kernel` returns an initial :data:`~numpyro.infer.mcmc.HMCState` that can be used to
            generate samples using MCMC. Else, returns the arguments and callable
            that does the initial adaptation.
        :param bool progbar: Whether to enable progress bar updates. Defaults to
            ``True``.
        :param jax.random.PRNGKey rng: random key to be used as the source of
            randomness.
        """
        step_size = lax.convert_element_type(step_size, xla_bridge.canonicalize_dtype(np.float64))
        nonlocal momentum_generator, wa_update, trajectory_len, max_treedepth, wa_steps
        wa_steps = num_warmup
        trajectory_len = trajectory_length
        max_treedepth = max_tree_depth
        z = init_params
        z_flat, unravel_fn = ravel_pytree(z)
        momentum_generator = partial(_sample_momentum, unravel_fn)

        find_reasonable_ss = partial(find_reasonable_step_size,
                                     potential_fn, kinetic_fn,
                                     momentum_generator)

        wa_init, wa_update = warmup_adapter(num_warmup,
                                            adapt_step_size=adapt_step_size,
                                            adapt_mass_matrix=adapt_mass_matrix,
                                            dense_mass=dense_mass,
                                            target_accept_prob=target_accept_prob,
                                            find_reasonable_step_size=find_reasonable_ss)

        rng_hmc, rng_wa = random.split(rng)
        wa_state = wa_init(z, rng_wa, step_size, mass_matrix_size=np.size(z_flat))
        r = momentum_generator(wa_state.mass_matrix_sqrt, rng)
        vv_state = vv_init(z, r)
        energy = kinetic_fn(wa_state.inverse_mass_matrix, vv_state.r)
        hmc_state = HMCState(0, vv_state.z, vv_state.z_grad, vv_state.potential_energy, energy,
                             0, 0., 0., False, wa_state, rng_hmc)

        # TODO: Remove; this should be the responsibility of the MCMC class.
        if run_warmup and num_warmup > 0:
            # JIT if progress bar updates not required
            if not progbar:
                hmc_state = fori_loop(0, num_warmup, lambda *args: sample_kernel(args[1]), hmc_state)
            else:
                with tqdm.trange(num_warmup, desc='warmup') as t:
                    for i in t:
                        hmc_state = jit(sample_kernel)(hmc_state)
                        t.set_postfix_str(get_diagnostics_str(hmc_state), refresh=False)
        return hmc_state

    def _hmc_next(step_size, inverse_mass_matrix, vv_state, rng):
        num_steps = _get_num_steps(step_size, trajectory_len)
        vv_state_new = fori_loop(0, num_steps,
                                 lambda i, val: vv_update(step_size, inverse_mass_matrix, val),
                                 vv_state)
        energy_old = vv_state.potential_energy + kinetic_fn(inverse_mass_matrix, vv_state.r)
        energy_new = vv_state_new.potential_energy + kinetic_fn(inverse_mass_matrix, vv_state_new.r)
        delta_energy = energy_new - energy_old
        delta_energy = np.where(np.isnan(delta_energy), np.inf, delta_energy)
        accept_prob = np.clip(np.exp(-delta_energy), a_max=1.0)
        diverging = delta_energy > max_delta_energy
        transition = random.bernoulli(rng, accept_prob)
        vv_state, energy = cond(transition,
                                (vv_state_new, energy_new), lambda args: args,
                                (vv_state, energy_old), lambda args: args)
        return vv_state, energy, num_steps, accept_prob, diverging

    def _nuts_next(step_size, inverse_mass_matrix, vv_state, rng):
        binary_tree = build_tree(vv_update, kinetic_fn, vv_state,
                                 inverse_mass_matrix, step_size, rng,
                                 max_delta_energy=max_delta_energy,
                                 max_tree_depth=max_treedepth)
        accept_prob = binary_tree.sum_accept_probs / binary_tree.num_proposals
        num_steps = binary_tree.num_proposals
        vv_state = IntegratorState(z=binary_tree.z_proposal,
                                   r=vv_state.r,
                                   potential_energy=binary_tree.z_proposal_pe,
                                   z_grad=binary_tree.z_proposal_grad)
        return vv_state, binary_tree.z_proposal_energy, num_steps, accept_prob, binary_tree.diverging

    _next = _nuts_next if algo == 'NUTS' else _hmc_next

    def sample_kernel(hmc_state):
        """
        Given an existing :data:`~numpyro.infer.mcmc.HMCState`, run HMC with fixed (possibly adapted)
        step size and return a new :data:`~numpyro.infer.mcmc.HMCState`.

        :param hmc_state: Current sample (and associated state).
        :return: new proposed :data:`~numpyro.infer.mcmc.HMCState` from simulating
            Hamiltonian dynamics given existing state.
        """
        rng, rng_momentum, rng_transition = random.split(hmc_state.rng, 3)
        r = momentum_generator(hmc_state.adapt_state.mass_matrix_sqrt, rng_momentum)
        vv_state = IntegratorState(hmc_state.z, r, hmc_state.potential_energy, hmc_state.z_grad)
        vv_state, energy, num_steps, accept_prob, diverging = _next(hmc_state.adapt_state.step_size,
                                                                    hmc_state.adapt_state.inverse_mass_matrix,
                                                                    vv_state, rng_transition)
        # not update adapt_state after warmup phase
        adapt_state = cond(hmc_state.i < wa_steps,
                           (hmc_state.i, accept_prob, vv_state.z, hmc_state.adapt_state),
                           lambda args: wa_update(*args),
                           hmc_state.adapt_state,
                           lambda x: x)

        itr = hmc_state.i + 1
        n = np.where(hmc_state.i < wa_steps, itr, itr - wa_steps)
        mean_accept_prob = hmc_state.mean_accept_prob + (accept_prob - hmc_state.mean_accept_prob) / n

        return HMCState(itr, vv_state.z, vv_state.z_grad, vv_state.potential_energy, energy, num_steps,
                        accept_prob, mean_accept_prob, diverging, adapt_state, rng)

    # Make `init_kernel` and `sample_kernel` visible from the global scope once
    # `hmc` is called for sphinx doc generation.
    if 'SPHINX_BUILD' in os.environ:
        hmc.init_kernel = init_kernel
        hmc.sample_kernel = sample_kernel

    return init_kernel, sample_kernel


def mcmc(num_warmup, num_samples, init_params, num_chains=1, sampler='hmc',
         constrain_fn=None, print_summary=True, **sampler_kwargs):
    """
    Convenience wrapper for MCMC samplers -- runs warmup, prints
    diagnostic summary and returns a collections of samples
    from the posterior.

    :param num_warmup: Number of warmup steps.
    :param num_samples: Number of samples to generate from the Markov chain.
    :param init_params: Initial parameters to begin sampling. The type
        must be consistent with the input type to `potential_fn`.
    :param sampler: currently, only `hmc` is implemented (default).
    :param constrain_fn: Callable that converts a collection of unconstrained
        sample values returned from the sampler to constrained values that
        lie within the support of the sample sites.
    :param print_summary: Whether to print diagnostics summary for
        each sample site. Default is ``True``.
    :param `**sampler_kwargs`: Sampler specific keyword arguments.

         - *HMC*: Refer to :func:`~numpyro.infer.mcmc.hmc` and
           :func:`~numpyro.infer.mcmc.hmc.init_kernel` for accepted arguments. Note
           that all arguments must be provided as keywords.

    :return: collection of samples from the posterior.

    .. testsetup::

       import jax
       from jax import random
       import jax.numpy as np
       import numpyro
       import numpyro.distributions as dist
       from numpyro.hmc_util import initialize_model
       from numpyro.infer.mcmc import hmc
       from numpyro.util import fori_collect

    .. doctest::

        >>> true_coefs = np.array([1., 2., 3.])
        >>> data = random.normal(random.PRNGKey(2), (2000, 3))
        >>> dim = 3
        >>> labels = dist.Bernoulli(logits=(true_coefs * data).sum(-1)).sample(random.PRNGKey(3))
        >>>
        >>> def model(data, labels):
        ...     coefs_mean = np.zeros(dim)
        ...     coefs = numpyro.sample('beta', dist.Normal(coefs_mean, np.ones(3)))
        ...     intercept = numpyro.sample('intercept', dist.Normal(0., 10.))
        ...     return numpyro.sample('y', dist.Bernoulli(logits=(coefs * data + intercept).sum(-1)), obs=labels)
        >>>
        >>> init_params, potential_fn, constrain_fn = initialize_model(random.PRNGKey(0), model,
        ...                                                            data, labels)
        >>> num_warmup, num_samples = 1000, 1000
        >>> samples = mcmc(num_warmup, num_samples, init_params,
        ...                potential_fn=potential_fn,
        ...                constrain_fn=constrain_fn)  # doctest: +SKIP
        warmup: 100%|██████████| 1000/1000 [00:09<00:00, 109.40it/s, 1 steps of size 5.83e-01. acc. prob=0.79]
        sample: 100%|██████████| 1000/1000 [00:00<00:00, 1252.39it/s, 1 steps of size 5.83e-01. acc. prob=0.85]


                           mean         sd       5.5%      94.5%      n_eff       Rhat
            coefs[0]       0.96       0.07       0.85       1.07     455.35       1.01
            coefs[1]       2.05       0.09       1.91       2.20     332.00       1.01
            coefs[2]       3.18       0.13       2.96       3.37     320.27       1.00
           intercept      -0.03       0.02      -0.06       0.00     402.53       1.00
    """
    warnings.warn("This interface to MCMC is deprecated and will be removed in the "
                  "next version. Please use `numpyro.infer.MCMC` instead.",
                  DeprecationWarning)
    sequential_chain = False
    if xla_bridge.device_count() < num_chains:
        sequential_chain = True
        warnings.warn('There are not enough devices to run parallel chains: expected {} but got {}.'
                      ' Chains will be drawn sequentially. If you are running `mcmc` in CPU,'
                      ' consider to disable XLA intra-op parallelism by setting the environment'
                      ' flag "XLA_FLAGS=--xla_force_host_platform_device_count={}".'
                      .format(num_chains, xla_bridge.device_count(), num_chains))
    progbar = sampler_kwargs.pop('progbar', True)
    if num_chains > 1:
        progbar = False

    if sampler == 'hmc':
        if constrain_fn is None:
            constrain_fn = identity
        potential_fn = sampler_kwargs.pop('potential_fn')
        kinetic_fn = sampler_kwargs.pop('kinetic_fn', None)
        algo = sampler_kwargs.pop('algo', 'NUTS')
        if num_chains > 1:
            rngs = sampler_kwargs.pop('rng', vmap(PRNGKey)(np.arange(num_chains)))
        else:
            rng = sampler_kwargs.pop('rng', PRNGKey(0))

        init_kernel, sample_kernel = hmc(potential_fn, kinetic_fn, algo)
        if progbar:
            hmc_state = init_kernel(init_params, num_warmup, progbar=progbar, rng=rng,
                                    **sampler_kwargs)
            samples_flat = fori_collect(0, num_samples, sample_kernel, hmc_state,
                                        transform=lambda x: constrain_fn(x.z),
                                        progbar=progbar,
                                        diagnostics_fn=get_diagnostics_str,
                                        progbar_desc=lambda x: 'sample')
            samples = tree_map(lambda x: x[np.newaxis, ...], samples_flat)
        else:
            def single_chain_mcmc(rng, init_params):
                hmc_state = init_kernel(init_params, num_warmup, run_warmup=False, rng=rng,
                                        **sampler_kwargs)
                samples = fori_collect(num_warmup, num_warmup + num_samples, sample_kernel, hmc_state,
                                       transform=lambda x: constrain_fn(x.z),
                                       progbar=progbar)
                return samples

            if num_chains == 1:
                samples_flat = single_chain_mcmc(rng, init_params)
                samples = tree_map(lambda x: x[np.newaxis, ...], samples_flat)
            else:
                if sequential_chain:
                    samples = lax.map(lambda args: single_chain_mcmc(*args), (rngs, init_params))
                else:
                    samples = pmap(single_chain_mcmc)(rngs, init_params)
                samples_flat = tree_map(lambda x: np.reshape(x, (-1,) + x.shape[2:]), samples)

        if print_summary:
            summary(samples)
        return samples_flat
    else:
        raise ValueError('sampler: {} not recognized'.format(sampler))


# ========= Higher Level API ========= #


class MCMCKernel(ABC):
    """
    Defines the interface for the Markov transition kernel that is
    used for :class:`~numpyro.infer.MCMC` inference.

    :param random.PRNGKey rng: Random number generator key to initialize
        the kernel.
    :param int num_warmup: Number of warmup steps. This can be useful
        when doing adaptation during warmup.
    :param tuple init_params: Initial parameters to begin sampling. The type must be consistent
            with the input type to `potential_fn`.
    :param model_args: Arguments provided to the model.
    :param model_kwargs: Keyword arguments provided to the model.
    """
    @abstractmethod
    def init(self, rng, num_warmup, init_params, model_args, model_kwargs):
        raise NotImplementedError

    @abstractmethod
    def sample(self, state):
        """
        Given the current `state`, return the next `state` using the given
        transition kernel.

        :param state: Arbitrary data structure representing the state for the
            kernel. For HMC, this is given by :data:`~numpyro.infer.mcmc.HMCState`.
        :return: Next `state`.
        """
        raise NotImplementedError


class HMC(MCMCKernel):
    """
    Hamiltonian Monte Carlo inference, using fixed trajectory length, with
    provision for step size and mass matrix adaptation.

    **References:**

    1. *MCMC Using Hamiltonian Dynamics*,
       Radford M. Neal

    :param model: Python callable containing Pyro :mod:`~numpyro.primitives`.
        If model is provided, `potential_fn` will be inferred using the model.
    :param potential_fn: Python callable that computes the potential energy
        given input parameters. The input parameters to `potential_fn` can be
        any python collection type, provided that `init_params` argument to
        `init_kernel` has the same type.
    :param kinetic_fn: Python callable that returns the kinetic energy given
        inverse mass matrix and momentum. If not provided, the default is
        euclidean kinetic energy.
    :param float step_size: Determines the size of a single step taken by the
        verlet integrator while computing the trajectory using Hamiltonian
        dynamics. If not specified, it will be set to 1.
    :param bool adapt_step_size: A flag to decide if we want to adapt step_size
        during warm-up phase using Dual Averaging scheme.
    :param bool adapt_mass_matrix: A flag to decide if we want to adapt mass
        matrix during warm-up phase using Welford scheme.
    :param bool dense_mass:  A flag to decide if mass matrix is dense or
        diagonal (default when ``dense_mass=False``)
    :param float target_accept_prob: Target acceptance probability for step size
        adaptation using Dual Averaging. Increasing this value will lead to a smaller
        step size, hence the sampling will be slower but more robust. Default to 0.8.
    :param float trajectory_length: Length of a MCMC trajectory for HMC. Default
        value is :math:`2\\pi`.
    """
    def __init__(self,
                 model=None,
                 potential_fn=None,
                 kinetic_fn=None,
                 step_size=1.0,
                 adapt_step_size=True,
                 adapt_mass_matrix=True,
                 dense_mass=False,
                 target_accept_prob=0.8,
                 trajectory_length=2*math.pi):
        if not (model is None) ^ (potential_fn is None):
            raise ValueError('Only one of `model` or `potential_fn` must be specified.')
        self.model = model
        self.potential_fn = potential_fn
        self.kinetic_fn = kinetic_fn if kinetic_fn is not None else euclidean_kinetic_energy
        self.step_size = step_size
        self.adapt_step_size = adapt_step_size
        self.adapt_mass_matrix = adapt_mass_matrix
        self.dense_mass = dense_mass
        self.target_accept_prob = target_accept_prob
        self.trajectory_length = trajectory_length
        self._sample_fn = None
        self.algo = 'HMC'
        self.max_tree_depth = 10

    @copy_docs_from(MCMCKernel.init)
    def init(self, rng, num_warmup, init_params=None, model_args=(), model_kwargs={}):
        constrain_fn = None
        if self.model is not None:
            if rng.ndim == 1:
                rng, rng_init_model = random.split(rng)
            else:
                rng, rng_init_model = np.swapaxes(vmap(random.split)(rng), 0, 1)
            init_params_, self.potential_fn, constrain_fn = initialize_model(rng_init_model, self.model,
                                                                             *model_args, **model_kwargs)
            if init_params is None:
                init_params = init_params_
        else:
            # User needs to provide valid `init_params` if using `potential_fn`.
            if init_params is None:
                raise ValueError('Valid value of `init_params` must be provided with'
                                 ' `potential_fn`.')
        hmc_init, sample_fn = hmc(self.potential_fn, self.kinetic_fn, algo=self.algo)
        hmc_init_fn = lambda init_params, rng: hmc_init(  # noqa: E731
            init_params,
            num_warmup=num_warmup,
            step_size=self.step_size,
            adapt_step_size=self.adapt_step_size,
            adapt_mass_matrix=self.adapt_mass_matrix,
            dense_mass=self.dense_mass,
            target_accept_prob=self.target_accept_prob,
            trajectory_length=self.trajectory_length,
            max_tree_depth=self.max_tree_depth,
            run_warmup=False,
            rng=rng,
        )
        if rng.ndim == 1:
            init_state = hmc_init_fn(init_params, rng)
            self._sample_fn = sample_fn
        else:
            # XXX it is safe to run hmc_init_fn under vmap despite that hmc_init_fn changes some
            # nonlocal variables: momentum_generator, wa_update, trajectory_len, max_treedepth,
            # wa_steps because those variables do not depend on traced args: init_params, rng.
            init_state = vmap(hmc_init_fn)(init_params, rng)
            self._sample_fn = vmap(sample_fn)
        return init_state, constrain_fn

    def sample(self, state):
        """
        Run HMC from the given :data:`~numpyro.infer.mcmc.HMCState` and return the resulting
        :data:`~numpyro.infer.mcmc.HMCState`.

        :param HMCState state: Represents the current state.
        :return: Next `state` after running HMC.
        """
        return self._sample_fn(state)


class NUTS(HMC):
    """
     Hamiltonian Monte Carlo inference, using the No U-Turn Sampler (NUTS)
     with adaptive path length and mass matrix adaptation.

    **References:**

    1. *MCMC Using Hamiltonian Dynamics*,
       Radford M. Neal
    2. *The No-U-turn sampler: adaptively setting path lengths in Hamiltonian Monte Carlo*,
       Matthew D. Hoffman, and Andrew Gelman.
    3. *A Conceptual Introduction to Hamiltonian Monte Carlo`*,
       Michael Betancourt

    :param model: Python callable containing Pyro :mod:`~numpyro.primitives`.
        If model is provided, `potential_fn` will be inferred using the model.
    :param potential_fn: Python callable that computes the potential energy
        given input parameters. The input parameters to `potential_fn` can be
        any python collection type, provided that `init_params` argument to
        `init_kernel` has the same type.
    :param kinetic_fn: Python callable that returns the kinetic energy given
        inverse mass matrix and momentum. If not provided, the default is
        euclidean kinetic energy.
    :param float step_size: Determines the size of a single step taken by the
        verlet integrator while computing the trajectory using Hamiltonian
        dynamics. If not specified, it will be set to 1.
    :param bool adapt_step_size: A flag to decide if we want to adapt step_size
        during warm-up phase using Dual Averaging scheme.
    :param bool adapt_mass_matrix: A flag to decide if we want to adapt mass
        matrix during warm-up phase using Welford scheme.
    :param bool dense_mass:  A flag to decide if mass matrix is dense or
        diagonal (default when ``dense_mass=False``)
    :param float target_accept_prob: Target acceptance probability for step size
        adaptation using Dual Averaging. Increasing this value will lead to a smaller
        step size, hence the sampling will be slower but more robust. Default to 0.8.
    :param float trajectory_length: Length of a MCMC trajectory for HMC. Default
        value is :math:`2\\pi`.
    :param int max_tree_depth: Max depth of the binary tree created during the doubling
        scheme of NUTS sampler. Defaults to 10.
    """
    def __init__(self,
                 model=None,
                 potential_fn=None,
                 kinetic_fn=None,
                 step_size=1.0,
                 adapt_step_size=True,
                 adapt_mass_matrix=True,
                 dense_mass=False,
                 target_accept_prob=0.8,
                 trajectory_length=2 * math.pi,
                 max_tree_depth=10):
        super(NUTS, self).__init__(potential_fn=potential_fn, model=model, kinetic_fn=kinetic_fn,
                                   step_size=step_size, adapt_step_size=adapt_step_size,
                                   adapt_mass_matrix=adapt_mass_matrix, dense_mass=dense_mass,
                                   target_accept_prob=target_accept_prob, trajectory_length=trajectory_length)
        self.max_tree_depth = max_tree_depth
        self.algo = 'NUTS'


def _laxmap(f, xs):
    n = tree_flatten(xs)[0][0].shape[0]

    def get_value_from_index(i):
        return tree_map(lambda x: x[i], xs)

    ys = []
    for i in range(n):
        x = jit(get_value_from_index)(i)
        ys.append(f(x))

    return tree_multimap(lambda *args: np.stack(args), *ys)


class MCMC(object):
    """
    Provides access to Markov Chain Monte Carlo inference algorithms in NumPyro.

    .. note:: `chain_method` is an experimental arg, which might be removed in a future version.

    .. note:: Setting `progress_bar=False` will improve the speed for many cases.

    :param MCMCKernel sampler: an instance of :class:`~numpyro.infer.mcmc.MCMCKernel` that
        determines the sampler for running MCMC. Currently, only :class:`~numpyro.infer.mcmc.HMC`
        and :class:`~numpyro.infer.mcmc.NUTS` are available.
    :param int num_warmup: Number of warmup steps.
    :param int num_samples: Number of samples to generate from the Markov chain.
    :param int num_chains: Number of Number of MCMC chains to run. By default,
        chains will be run in parallel using :func:`jax.pmap`, failing which,
        chains will be run in sequence.
    :param constrain_fn: Callable that converts a collection of unconstrained
        sample values returned from the sampler to constrained values that
        lie within the support of the sample sites.
    :param str chain_method: One of 'parallel' (default), 'sequential', 'vectorized'. The method
        'parallel' is used to execute the drawing process in parallel on XLA devices (CPUs/GPUs/TPUs),
        If there are not enough devices for 'parallel', we fall back to 'sequential' method to draw
        chains sequentially. 'vectorized' method is an experimental feature which vectorizes the
        drawing method, hence allowing us to collect samples in parallel on a single device.
    :param bool progress_bar: Whether to enable progress bar updates. Defaults to
        ``True``.
    """
    def __init__(self,
                 sampler,
                 num_warmup,
                 num_samples,
                 num_chains=1,
                 constrain_fn=None,
                 chain_method='parallel',
                 progress_bar=True):
        self.sampler = sampler
        self.num_warmup = num_warmup
        self.num_samples = num_samples
        self.num_chains = num_chains
        self.constrain_fn = constrain_fn
        self.chain_method = chain_method
        self.progress_bar = progress_bar
        # TODO: We should have progress bars (maybe without diagnostics) for num_chains > 1
        if (chain_method == 'parallel' and num_chains > 1) or (
                "CI" in os.environ or "PYTEST_XDIST_WORKER" in os.environ):
            self.progress_bar = False

        self._collect_fields = ('z',)
        self._samples = None
        self._samples_flat = None

    def _single_chain_mcmc(self, init, collect_fields=('z',), collect_warmup=False, args=(), kwargs={}):
        rng, init_params = init
        hmc_state, constrain_fn = self.sampler.init(rng, self.num_warmup, init_params,
                                                    model_args=args, model_kwargs=kwargs)
        if self.constrain_fn is None:
            constrain_fn = identity if constrain_fn is None else constrain_fn
        else:
            constrain_fn = self.constrain_fn
        collect_fn = attrgetter(*collect_fields)
        lower = 0 if collect_warmup else self.num_warmup
        samples = fori_collect(lower, self.num_warmup + self.num_samples,
                               self.sampler.sample,
                               hmc_state,
                               transform=collect_fn,
                               progbar=self.progress_bar,
                               progbar_desc=functools.partial(get_progbar_desc_str, self.num_warmup),
                               diagnostics_fn=get_diagnostics_str if rng.ndim == 1 else None)
        if len(collect_fields) == 1:
            samples = (samples,)
        samples = dict(zip(collect_fields, samples))
        samples['z'] = vmap(constrain_fn)(samples['z']) if len(tree_flatten(samples)[0]) > 0 else samples['z']
        return samples

    def run(self, rng, *args, collect_fields=('z',), collect_warmup=False, init_params=None, **kwargs):
        """
        Run the MCMC samplers and collect samples.

        :param random.PRNGKey rng: Random number generator key to be used for the sampling.
        :param args: Arguments to be provided to the :meth:`numpyro.infer.mcmc.MCMCKernel.init` method.
            These are typically the arguments needed by the `model`.
        :param collect_fields: Fields from :data:`numpyro.infer.mcmc.HMCState` to collect
            during the MCMC run. By default, only the latent sample sites `z` is collected.
        :type collect_fields: tuple or list
        :param bool collect_warmup: Whether to collect samples from the warmup phase. Defaults
            to `False`.
        :param init_params: Initial parameters to begin sampling. The type must be consistent
            with the input type to `potential_fn`.
        :param kwargs: Keyword arguments to be provided to the :meth:`numpyro.infer.mcmc.MCMCKernel.init`
            method. These are typically the keyword arguments needed by the `model`.
        """
        self._args = args
        self._kwargs = kwargs
        chain_method = self.chain_method
        if chain_method == 'parallel' and xla_bridge.device_count() < self.num_chains:
            chain_method = 'sequential'
            warnings.warn('There are not enough devices to run parallel chains: expected {} but got {}.'
                          ' Chains will be drawn sequentially. If you are running MCMC in CPU,'
                          ' consider to use `numpyro.util.set_host_devices({})` at the beginning'
                          ' of your program.'
                          .format(self.num_chains, xla_bridge.device_count(), self.num_chains))

        if init_params is not None and self.num_chains > 1:
            prototype_init_val = tree_flatten(init_params)[0][0]
            if np.shape(prototype_init_val)[0] != self.num_chains:
                raise ValueError('`init_params` must have the same leading dimension'
                                 ' as `num_chains`.')
        assert isinstance(collect_fields, (tuple, list))
        self._collect_fields = collect_fields
        if self.num_chains == 1:
            samples_flat = self._single_chain_mcmc((rng, init_params), collect_fields, collect_warmup,
                                                   args, kwargs)
            samples = tree_map(lambda x: x[np.newaxis, ...], samples_flat)
        else:
            rngs = random.split(rng, self.num_chains)
            partial_map_fn = partial(self._single_chain_mcmc,
                                     collect_fields=collect_fields,
                                     collect_warmup=collect_warmup,
                                     args=args,
                                     kwargs=kwargs)
            if chain_method == 'sequential':
                if self.progress_bar:
                    map_fn = partial(_laxmap, partial_map_fn)
                else:
                    map_fn = partial(lax.map, partial_map_fn)
            elif chain_method == 'parallel':
                map_fn = pmap(partial_map_fn)
            elif chain_method == 'vectorized':
                map_fn = partial_map_fn
            else:
                raise ValueError('Only supporting the following methods to draw chains:'
                                 ' "sequential", "parallel", or "vectorized"')
            samples = map_fn((rngs, init_params))
            samples_flat = tree_map(lambda x: np.reshape(x, (-1,) + x.shape[2:]), samples)
        self._samples = samples
        self._samples_flat = samples_flat

    def get_samples(self, group_by_chain=False):
        """
        Get samples from the MCMC run.

        :param bool group_by_chain: Whether to preserve the chain dimension. If True,
            all samples will have num_chains as the size of their leading dimension.
        :return: Samples having the same data type as `init_params`. If multiple fields
            are collected via the `collect_fields` arg to :meth:`~numpyro.infer.mcmc.MCMC.run`,
            then a tuple with the same data type is returned, one for each of the fields.
            The data type for a particular field is a `dict` keyed on site names if a
            model containing Pyro primitives is used, but can be any :func:`jaxlib.pytree`,
            more generally (e.g. when defining a `potential_fn` for HMC that takes
            `list` args).
        """
        get_items = itemgetter(*self._collect_fields)
        return get_items(self._samples) if group_by_chain else get_items(self._samples_flat)

    def print_summary(self, prob=0.9):
        if 'z' not in self._samples:
            raise ValueError('No latent samples `z` collected. Pass `z` to `collect_fields` arg.')
        summary(self._samples['z'], prob=prob)
