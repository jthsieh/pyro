from __future__ import absolute_import, division, print_function

import math
import numbers
from collections import defaultdict

import torch

from pyro.distributions.util import is_identically_zero
from pyro.poutine.util import site_is_subsample


def torch_exp(x):
    """
    Like ``x.exp()`` for a :class:`~torch.Tensor`, but also accepts
    numbers.
    """
    if isinstance(x, numbers.Number):
        return math.exp(x)
    return x.exp()


def torch_data_sum(x):
    """
    Like ``x.sum().item()`` for a :class:`~torch.Tensor`, but also works
    with numbers.
    """
    if isinstance(x, numbers.Number):
        return x
    return x.sum().item()


def torch_backward(x):
    """
    Like ``x.backward()`` for a :class:`~torch.Tensor`, but also accepts
    numbers (a no-op if given a number).
    """
    if torch.is_tensor(x):
        x.backward()


def reduce_to_target(source, target):
    """
    Sums out any dimensions in source that are of size > 1 in source but of
    size 1 in target.
    """
    while source.dim() > target.dim():
        source = source.sum(0)
    for k in range(1, 1 + source.dim()):
        if source.size(-k) > target.size(-k):
            source = source.sum(-k, keepdim=True)
    return source


def reduce_to_shape(source, shape):
    """
    Sums out any dimensions in source that are of size > 1 in source but of
    size 1 in target.
    """
    while source.dim() > len(shape):
        source = source.sum(0)
    for k in range(1, 1 + source.dim()):
        if source.size(-k) > shape[-k]:
            source = source.sum(-k, keepdim=True)
    return source


def get_iarange_stacks(trace):
    """
    This builds a dict mapping site name to a set of iarange stacks.  Each
    iarange stack is a list of :class:`CondIndepStackFrame`s corresponding to
    an :class:`iarange`.  This information is used by :class:`Trace_ELBO` and
    :class:`TraceGraph_ELBO`.
    """
    return {name: [f for f in node["cond_indep_stack"] if f.vectorized]
            for name, node in trace.nodes.items()
            if node["type"] == "sample" and not site_is_subsample(node)}


class MultiFrameTensor(dict):
    """
    A container for sums of Tensors among different :class:`iarange` contexts.

    Used in :class:`~pyro.infer.tracegraph_elbo.TraceGraph_ELBO` to simplify
    downstream cost computation logic.

    Example::

        downstream_cost = MultiFrameTensor()
        for site in downstream_nodes:
            downstream_cost.add((site["cond_indep_stack"], site["log_prob"]))
        downstream_cost.add(*other_costs.items())  # add in bulk
        summed = downstream_cost.sum_to(target_site["cond_indep_stack"])
    """
    def __init__(self, *items):
        super(MultiFrameTensor, self).__init__()
        self.add(*items)

    def add(self, *items):
        """
        Add a collection of (cond_indep_stack, tensor) pairs. Keys are
        ``cond_indep_stack``s, i.e. tuples of :class:`CondIndepStackFrame`s.
        Values are :class:`torch.Tensor`s.
        """
        for cond_indep_stack, value in items:
            frames = frozenset(f for f in cond_indep_stack if f.vectorized)
            assert all(f.dim < 0 and -len(value.shape) <= f.dim for f in frames)
            if frames in self:
                self[frames] = self[frames] + value
            else:
                self[frames] = value

    def sum_to(self, target_frames):
        total = None
        for frames, value in self.items():
            for f in frames:
                if f not in target_frames and value.shape[f.dim] != 1:
                    value = value.sum(f.dim, True)
            while value.shape and value.shape[0] == 1:
                value.squeeze_(0)
            total = value if total is None else total + value
        return total

    def __repr__(self):
        return '%s(%s)' % (type(self).__name__, ",\n\t".join([
            '({}, ...)'.format(frames) for frames in self]))


class Dice(object):
    """
    An implementation of the DiCE operator compatible with Pyro features.

    This implementation correctly handles:
    - scaled log-probability due to subsampling
    - independence in different ordinals due to iarange
    - weights due to parallel and sequential enumeration

    This assumes restricted dependency structure on the model and guide:
    variables outside of an :class:`~pyro.iarange` can never depend on
    variables inside that :class:`~pyro.iarange`.

    Refereces:
    [1] Jakob Foerster, Greg Farquhar, Maruan Al-Shedivat, Tim Rocktaeschel,
        Eric P. Xing, Shimon Whiteson (2018)
        "DiCE: The Infinitely Differentiable Monte-Carlo Estimator"
        https://arxiv.org/abs/1802.05098
    """
    def __init__(self, guide_trace, ordering):
        log_denom = defaultdict(lambda: 0.0)  # avoids double-counting when sequentially enumerating
        log_probs = defaultdict(list)  # accounts for upstream probabilties

        for name, site in guide_trace.nodes.items():
            if site["type"] != "sample":
                continue
            log_prob = site['score_parts'].score_function  # not scaled by subsampling
            if is_identically_zero(log_prob):
                continue

            ordinal = ordering[name]
            if site["infer"].get("enumerate"):
                if site["infer"]["enumerate"] == "sequential":
                    log_denom[ordinal] += math.log(site["infer"]["_enum_total"])
            else:  # site was monte carlo sampled
                log_prob = log_prob - log_prob.detach()
            log_probs[ordinal].append(log_prob)

        self.log_denom = log_denom
        self.log_probs = log_probs
        self._log_factors_cache = {}
        self._dice_prob_cache = {}

    def get_log_factors(self, target_ordinal):
        """
        Returns a list of DiCE factors ordinal.
        """
        try:
            return self._log_factors_cache[target_ordinal]
        except KeyError:
            pass

        log_denom = 0
        for ordinal, term in self.log_denom.items():
            if not ordinal <= target_ordinal:  # not downstream
                log_denom += term  # term = log(# times this ordinal is counted)

        log_factors = [] if is_identically_zero(log_denom) else [-log_denom]
        for ordinal, term in self.log_probs.items():
            if ordinal <= target_ordinal:  # upstream
                log_factors += term  # term = [log(dice weight of this ordinal)]

        self._log_factors_cache[target_ordinal] = log_factors
        return log_factors

    def get_dice_prob(self, shape, ordinal):
        """
        Returns the DiCE operator at a given ordinal, summed to given shape.
        """
        try:
            return self._dice_prob_cache[shape, ordinal]
        except KeyError:
            pass

        log_factors = self.get_log_factors(ordinal)

        # TODO replace this naive sum-product computation with message passing.
        dice_prob = sum(log_factors).exp()

        self._dice_prob_cache[ordinal] = dice_prob
        return dice_prob

    def expectation(self, cost, ordinal):
        dice_prob = self.get_dice_prob(cost.shape, ordinal)
        return (dice_prob * cost).sum()
