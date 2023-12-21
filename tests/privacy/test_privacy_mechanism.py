# -*- coding: utf-8 -*-

# Copyright © 2023-2024 Apple Inc.
'''
Test privacy_mechanism.py.
'''

import math
import typing
from unittest.mock import ANY, MagicMock

import numpy as np
import pytest
from pytest_lazyfixture import lazy_fixture

from pfl.common_types import Population
from pfl.context import CentralContext, UserContext
from pfl.hyperparam import ModelHyperParams
from pfl.hyperparam.base import AlgorithmHyperParams
from pfl.internal.ops import get_pytorch_major_version, get_tf_major_version
from pfl.internal.priv_unit_functions import compute_optimal_cap_offset
from pfl.metrics import Metrics, get_overall_value
from pfl.privacy import compute_parameters
from pfl.privacy.approximate_mechanism import SquaredErrorLocalPrivacyMechanism
from pfl.privacy.gaussian_mechanism import GaussianMechanism
from pfl.privacy.laplace_mechanism import LaplaceMechanism
from pfl.privacy.priv_unit_mechanism import PrivUnitMechanism
from pfl.privacy.privacy_accountant import PLDPrivacyAccountant
from pfl.privacy.privacy_mechanism import (
    CentrallyAppliedPrivacyMechanism,
    NoPrivacy,
    NormClippingOnly,
    PrivacyMetricName,
)
from pfl.stats import MappedVectorStatistics

# These fixtures set the internal framework module.
framework_fixtures = [
    pytest.param(lazy_fixture('numpy_ops')),
    pytest.param(lazy_fixture('tensorflow_ops'),
                 marks=[
                     pytest.mark.skipif(get_tf_major_version() < 2,
                                        reason='not tf>=2')
                 ]),
    pytest.param(lazy_fixture('pytorch_ops'),
                 marks=[
                     pytest.mark.skipif(not get_pytorch_major_version(),
                                        reason='PyTorch not installed')
                 ]),
]

_num_iterations = 700
_epsilon = 2.0
_delta = 1e-4

# False positive when initializing PrivacyMetricName.
# pytype: disable=duplicate-keyword-argument,wrong-arg-count


class TestMechanisms:

    def _get_values_l2_norm(self, shapes):
        size = sum([np.prod(shape) for shape in shapes])
        return math.sqrt(np.sum(np.square(np.arange(size))))

    # Test NoPrivacy.
    def _get_values(self, shapes):
        values = {}
        current_value = 0
        for name, shape in enumerate(shapes):
            size = 1
            for dimension in shape[1:]:
                size *= int(dimension)
            values[str(name)] = np.broadcast_to(
                np.reshape(
                    np.arange(float(current_value),
                              float(current_value + size)), shape[1:]),
                shape).astype(np.float32)
            current_value += size
        return MappedVectorStatistics(values)

    def test_no_privacy(self):
        mechanism = NoPrivacy()

        shapes = [(2, 2, 2), (2, 5, 3)]

        input_statistics = self._get_values(shapes)

        noised_values, metrics = mechanism.postprocess_one_user(
            stats=input_statistics, user_context=MagicMock())
        assert len(metrics) == 0

        for name in input_statistics:
            assert np.allclose(
                typing.cast(MappedVectorStatistics, noised_values)[name],
                input_statistics[name])

    # Test norm clipping.

    def _compute_l1_norm(self, statistics):
        return sum(np.sum(np.abs(value)) for value in statistics.values())

    def _compute_l2_norm(self, statistics):
        return math.sqrt(
            sum(np.sum(np.square(value)) for value in statistics.values()))

    def _get_norm_clipped_values(self, norm, stats, norm_bound):
        if norm <= norm_bound:
            return 1.0, stats
        else:
            factor = norm_bound / norm
            return factor, stats.apply_elementwise(lambda v: v * factor)

    def _to_tensor_stats(self, stats, ops):
        return stats.apply_elementwise(ops.to_tensor)

    def _from_tensor_stats(self, stats, ops):
        return stats.apply_elementwise(ops.to_numpy)

    def _check_norm_clipping(self, mechanism, shapes, norm_function,
                             ops_module):
        input_statistics = self._get_values(shapes)
        tensor_input_stats = self._to_tensor_stats(input_statistics,
                                                   ops_module)

        _, expected_clipped_stats = self._get_norm_clipped_values(
            norm_function(input_statistics), input_statistics,
            mechanism.clipping_bound)

        clipped_tensor_stats, _ = mechanism.constrain_sensitivity(
            tensor_input_stats)
        clipped_stats = self._from_tensor_stats(clipped_tensor_stats,
                                                ops_module)

        for name in input_statistics:
            assert np.allclose(
                typing.cast(MappedVectorStatistics, clipped_stats)[name],
                expected_clipped_stats[name])

    @pytest.mark.parametrize('ops_module', framework_fixtures)
    @pytest.mark.parametrize('norm_bound', [.1, 1., 1000.])
    def test_norm_clipping(self, ops_module, norm_bound):
        shapes = [(2, 2, 2), (2, 5, 3)]

        for mechanism, norm_function in [
            (NormClippingOnly(1, norm_bound), self._compute_l1_norm),
            (NormClippingOnly(2, norm_bound), self._compute_l2_norm)
        ]:
            self._check_norm_clipping(mechanism, shapes, norm_function,
                                      ops_module)

    def _check_mechanism_properties(self,
                                    mechanism,
                                    norm_function,
                                    shapes,
                                    epsilon,
                                    delta,
                                    norm_bound,
                                    expected_sigma,
                                    expected_kurtosis,
                                    expected_metrics,
                                    num_iterations,
                                    has_squared_error,
                                    expect_clipping_metrics=True,
                                    set_seed=False,
                                    ops=None):

        input_stats = self._get_values(shapes)
        input_tensor_stats = self._to_tensor_stats(input_stats, ops)

        var_names = list(input_stats.keys())

        num_dimensions = input_stats.num_parameters
        if norm_function is None:
            clip_factor = 1
            clipped_input_stats = input_stats
        else:
            clip_factor, clipped_input_stats = self._get_norm_clipped_values(
                norm_function(input_stats), input_stats, norm_bound)

        if has_squared_error:
            assert isinstance(mechanism, SquaredErrorLocalPrivacyMechanism)
            # Check that the theoretical squared error is correct.
            expected_add_noise_squared_error = ((expected_sigma**2) *
                                                num_dimensions)
            theoretical_add_noise_squared_error = mechanism.get_squared_error(
                num_dimensions, norm_function(input_stats), cohort_size=1)
            assert (theoretical_add_noise_squared_error == pytest.approx(
                expected_add_noise_squared_error, rel=1e-4))

        def privatize_and_get_stats(seed):
            """
            Compute statistics from one privatization.
            The statistics are the means over the first dimension, for better
            statistical power.
            """
            noised_arrays, metrics = mechanism.postprocess_one_user(
                stats=input_tensor_stats, user_context=MagicMock(seed=seed))
            noised_arrays = self._from_tensor_stats(noised_arrays, ops)

            # arrays after adding noise should have same shape as before
            for name in var_names:
                assert input_stats[name].shape == noised_arrays[name].shape

            noised_sum_arrays = [
                np.mean(noised_arrays[name], axis=0) for name in var_names
            ]

            square_deviation_arrays = [
                np.mean(np.square(noised_arrays[n] -
                                  (clip_factor * input_stats[n])),
                        axis=0) for n in var_names
            ]

            fourth_pow_deviation_arrays = [
                np.mean(np.power(
                    noised_arrays[n] - (clip_factor * input_stats[n]), 4),
                        axis=0) / np.square(square_deviation_arrays[i])
                for i, n in enumerate(var_names)
            ]

            return (noised_sum_arrays, square_deviation_arrays,
                    fourth_pow_deviation_arrays, metrics)

        sum_values = [np.zeros(input_stats[n].shape[1:]) for n in var_names]
        sum_square_values = [
            np.zeros(input_stats[n].shape[1:]) for n in var_names
        ]
        sum_fourth_pow_values = [
            np.zeros(input_stats[n].shape[1:]) for n in var_names
        ]
        for i in range(num_iterations):

            (noised_sum_arrays, square_deviation_arrays,
             fourth_pow_deviation_arrays,
             metrics) = privatize_and_get_stats(i if set_seed else None)
            # s references Numpy arrays here, so changing it changes
            # sum_values / sum_square_values.
            for s, v in zip(sum_values, noised_sum_arrays):
                s += v
            for s, v in zip(sum_square_values, square_deviation_arrays):
                s += v
            for s, v in zip(sum_fourth_pow_values,
                            fourth_pow_deviation_arrays):
                s += v

        # Test the metrics that are output.
        # How often the norm was clipped is either nothing or all in this test.
        if norm_function is not None:
            expected_metrics[PrivacyMetricName(
                'fraction of clipped norms',
                is_local_privacy=True)] = int(clip_factor < 1)
            expected_metrics[PrivacyMetricName(
                'norm before clipping',
                is_local_privacy=True)] = norm_function(input_stats)

        l1_name = PrivacyMetricName('l1 norm bound', is_local_privacy=True)
        l2_name = PrivacyMetricName('l2 norm bound', is_local_privacy=True)
        if l1_name in metrics:
            assert np.isclose(get_overall_value(metrics[l1_name]), norm_bound)
        else:
            assert l2_name in metrics
            assert np.isclose(get_overall_value(metrics[l2_name]), norm_bound)

        for name, expected_metric in expected_metrics:
            assert np.isclose(get_overall_value(metrics[name]),
                              expected_metric,
                              rtol=0.0001)

        noised_values_1d = np.hstack([
            np.reshape(value, (-1, )) / num_iterations for value in sum_values
        ])
        square_values_1d = np.hstack([
            np.reshape(value, (-1, )) / num_iterations
            for value in sum_square_values
        ])
        fourth_pow_values_1d = np.hstack([
            np.reshape(value, (-1, )) / num_iterations
            for value in sum_fourth_pow_values
        ])

        # convert from kurtosis to ex-kurtosis
        kurtosis_values = fourth_pow_values_1d - 3

        flat_expected_values = np.hstack(
            [clipped_input_stats[n][0, :].reshape(-1) for n in var_names])
        if expected_sigma is not None:
            # test moments
            assert np.allclose(noised_values_1d,
                               flat_expected_values,
                               atol=expected_sigma * 0.01)

            assert np.allclose(square_values_1d, expected_sigma**2, rtol=0.01)

        if expected_kurtosis is not None:
            assert np.allclose(kurtosis_values, expected_kurtosis, atol=0.1)

        if set_seed:
            # Check that the same seed always yields the same results.
            seed = 123
            context = MagicMock(seed=None)
            noise_1, _ = mechanism.postprocess_one_user(
                stats=input_tensor_stats, user_context=context)
            noise_2, _ = mechanism.postprocess_one_user(
                stats=input_tensor_stats, user_context=context)

            # With seed: the same results.
            context = MagicMock(seed=seed)
            seeded_noise_1, _ = mechanism.postprocess_one_user(
                stats=input_tensor_stats, user_context=context)
            seeded_noise_2, _ = mechanism.postprocess_one_user(
                stats=input_tensor_stats, user_context=context)

            noise_1 = self._from_tensor_stats(noise_1, ops)
            noise_2 = self._from_tensor_stats(noise_2, ops)
            seeded_noise_1 = self._from_tensor_stats(seeded_noise_1, ops)
            seeded_noise_2 = self._from_tensor_stats(seeded_noise_2, ops)

            for v1, v2 in zip(seeded_noise_1.get_weights()[1],
                              seeded_noise_2.get_weights()[1]):
                assert np.array_equal(v1, v2)

            # Without seed: different results.
            for v1, v2 in zip(noise_1.get_weights()[1],
                              noise_2.get_weights()[1]):
                assert (np.any(np.not_equal(v1, v2)))

    def _single_iteration_sigma(self, norm_bound, noise_scale):
        # This ought to be the correct sigma.
        sigma = compute_parameters.AnalyticGM_robust(_epsilon, _delta, 1,
                                                     norm_bound)
        # Check that this is the case though.
        delta_check = pytest.gaussian_mechanism_minimum_delta(
            _epsilon, sigma, norm_bound)
        assert delta_check == pytest.approx(_delta)

        mechanism = GaussianMechanism.construct_single_iteration(
            norm_bound, _epsilon, _delta)
        return (mechanism, sigma)

    def _from_privacy_accountant_sigma(self, norm_bound, noise_scale):
        privacy_accountant = PLDPrivacyAccountant(num_compositions=100,
                                                  sampling_probability=0.01,
                                                  mechanism='gaussian',
                                                  epsilon=2,
                                                  delta=1e-8,
                                                  noise_parameter=None,
                                                  noise_scale=noise_scale)
        mechanism = GaussianMechanism.from_privacy_accountant(
            accountant=privacy_accountant, clipping_bound=norm_bound)
        return (mechanism, mechanism.relative_noise_stddev * norm_bound)

    def test_privacy_accountant_noise_scalar(self):
        assert (self._from_privacy_accountant_sigma(
            1.0,
            1.0)[1] == self._from_privacy_accountant_sigma(1.0, 0.1)[1] * 10)

    @pytest.mark.parametrize('ops_module', framework_fixtures)
    @pytest.mark.parametrize('set_seed', [False, True])
    @pytest.mark.parametrize('norm_bound,noise_scale',
                             [(0.02, 1.0), (6e6, 1.0), (0.5, 0.1)])
    def test_gaussian_mechanism(self, ops_module, set_seed, norm_bound,
                                noise_scale, fix_global_random_seeds):
        shapes = [(300, 2, 2), (300, 5, 3)]
        # Two ways of constructing the mechanism: single-iteration, or using
        # the privacy accountant.
        for get_mechanism_sigma in [
                self._single_iteration_sigma,
                self._from_privacy_accountant_sigma
        ]:
            mechanism, sigma = get_mechanism_sigma(norm_bound, noise_scale)

            # kurtosis of a Gaussian distribution
            kurtosis = 0

            expected_metrics = Metrics([
                (PrivacyMetricName('DP noise std. dev.',
                                   is_local_privacy=True), sigma)
            ])

            self._check_mechanism_properties(mechanism,
                                             self._compute_l2_norm,
                                             shapes,
                                             _epsilon,
                                             _delta,
                                             norm_bound,
                                             sigma,
                                             kurtosis,
                                             expected_metrics,
                                             _num_iterations,
                                             set_seed=set_seed,
                                             has_squared_error=True,
                                             ops=ops_module)

    @pytest.mark.parametrize('ops_module', framework_fixtures)
    @pytest.mark.parametrize('set_seed', [False, True])
    @pytest.mark.parametrize('norm_bound', [0.02, 6e6])
    def test_laplace_mechanism(self, norm_bound, set_seed, ops_module,
                               fix_global_random_seeds):
        shapes = [(3000, 2, 2), (3000, 5, 3)]

        mechanism = LaplaceMechanism(norm_bound, _epsilon)

        # standard deviation of a Laplace distribution
        b = norm_bound / _epsilon
        sigma = np.sqrt(2 * b**2)

        # kurtosis of a Laplace distribution
        kurtosis = 3

        # values to compare tracked metrics with
        expected_metrics = Metrics([
            (PrivacyMetricName('Laplace DP noise scale',
                               is_local_privacy=True), b)
        ])

        self._check_mechanism_properties(mechanism,
                                         self._compute_l1_norm,
                                         shapes,
                                         _epsilon,
                                         0,
                                         norm_bound,
                                         sigma,
                                         kurtosis,
                                         expected_metrics,
                                         _num_iterations,
                                         has_squared_error=True,
                                         set_seed=set_seed,
                                         ops=ops_module)

    @pytest.mark.parametrize('ops_module', framework_fixtures)
    @pytest.mark.parametrize('set_seed', [False, True])
    @pytest.mark.parametrize('norm_bound', [0.02, 6e6])
    def test_priv_unit_mechanism(self, norm_bound, set_seed, ops_module,
                                 fix_global_random_seeds):
        shapes = [(300, 2, 2), (300, 5, 3)]
        l2_norm = self._get_values_l2_norm(shapes)
        num_dimensions = sum([np.prod(shape) for shape in shapes])
        mechanism = PrivUnitMechanism(norm_bound, _epsilon)

        scaling, _ = compute_optimal_cap_offset(_epsilon, num_dimensions)
        reference_bias = ((l2_norm /
                           norm_bound) if l2_norm < norm_bound else 1)
        squared_error = (scaling**2).value - reference_bias**2
        theoretical_squared_error = (scaling**2).value - 1**2
        sigma = math.sqrt(squared_error / num_dimensions) * norm_bound

        expected_metrics = Metrics([
            (PrivacyMetricName('DP squared error', is_local_privacy=True),
             theoretical_squared_error * (norm_bound**2))
        ])

        self._check_mechanism_properties(mechanism=mechanism,
                                         norm_function=self._compute_l2_norm,
                                         shapes=shapes,
                                         epsilon=_epsilon,
                                         delta=_delta,
                                         norm_bound=norm_bound,
                                         expected_sigma=sigma,
                                         expected_kurtosis=None,
                                         expected_metrics=expected_metrics,
                                         num_iterations=_num_iterations,
                                         has_squared_error=True,
                                         set_seed=set_seed,
                                         ops=ops_module)

    def test_central_mechanism(self, mock_privacy_mechanism):

        stats = MappedVectorStatistics({'a': np.arange(3)})
        user_context = UserContext(2, seed=1)

        central_mechanism = CentrallyAppliedPrivacyMechanism(
            mock_privacy_mechanism)
        stats_out, metrics = central_mechanism.postprocess_one_user(
            stats=stats, user_context=user_context)
        assert stats_out is stats
        assert metrics.to_simple_dict() == {
            'Central DP | constrain_sensitivity': 1.0
        }
        mock_privacy_mechanism.constrain_sensitivity.assert_called_once_with(
            statistics=stats, name_formatting_fn=ANY, seed=1)

        cohort_size = 4
        central_context = CentralContext(
            current_central_iteration=0,
            do_evaluation=True,
            population=Population.TRAIN,
            cohort_size=cohort_size,
            algorithm_params=AlgorithmHyperParams(),
            model_train_params=ModelHyperParams(),
            model_eval_params=ModelHyperParams(),
            seed=5)

        stats_out, metrics = central_mechanism.postprocess_server(
            stats=stats,
            central_context=central_context,
            aggregate_metrics=Metrics())
        assert stats_out is stats
        assert metrics.to_simple_dict() == {
            'Central DP | add_noise on summed stats': 1.0
        }
        mock_privacy_mechanism.add_noise.assert_called_once_with(
            statistics=stats,
            cohort_size=cohort_size,
            name_formatting_fn=ANY,
            seed=5)
        mock_privacy_mechanism.privatize.assert_not_called()

    @pytest.mark.parametrize(('order', 'clipping_bound', 'expected_arrays',
                              'expected_clip_fraction', 'expected_norm_bound'),
                             [
                                 (1, 6.01, {
                                     'a': np.array([1., 2.]),
                                     'b': np.array([3.])
                                 }, 0., 6.01),
                                 (1, 6, {
                                     'a': np.array([1., 2.]),
                                     'b': np.array([3.])
                                 }, 0., 6.0),
                                 (1, 5.99, {
                                     'a': 5.99 / 6 * np.array([1., 2.]),
                                     'b': 5.99 / 6 * np.array([3.])
                                 }, 1.0, 5.99),
                                 (np.inf, 3.01, {
                                     'a': np.array([1., 2.]),
                                     'b': np.array([3.])
                                 }, 0., 3.01),
                                 (np.inf, 3.0, {
                                     'a': np.array([1., 2.]),
                                     'b': np.array([3.])
                                 }, 0., 3.0),
                                 (np.inf, 2.99, {
                                     'a': 2.99 / 3 * np.array([1., 2.]),
                                     'b': 2.99 / 3 * np.array([3.])
                                 }, 1.0, 2.99),
                             ])
    def test_norm_clipping_only(self, order, clipping_bound, expected_arrays,
                                expected_clip_fraction, expected_norm_bound):
        norm_clipping = NormClippingOnly(order, clipping_bound)
        noisy, metrics = norm_clipping.postprocess_one_user(
            stats=MappedVectorStatistics({
                'a': np.array([1., 2.]),
                'b': np.array([3.])
            }),
            user_context=MagicMock())

        for name in expected_arrays:
            assert np.allclose(
                typing.cast(MappedVectorStatistics, noisy)[name],
                expected_arrays[name])

        assert len(metrics) == 3
        name = PrivacyMetricName(f'l{order:.0f} norm bound',
                                 is_local_privacy=True)
        assert get_overall_value(
            metrics[name]) == pytest.approx(expected_norm_bound)

        name = PrivacyMetricName('fraction of clipped norms',
                                 is_local_privacy=True)
        assert get_overall_value(
            metrics[name]) == pytest.approx(expected_clip_fraction)
