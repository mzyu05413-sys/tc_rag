"""独立验证数值、排序语义、异常输入和归一化恒等式。"""

import numpy as np
import pytest

from tc_rag.infrastructure.retrieval.metrics import get_metric


@pytest.mark.parametrize("dimension", [1, 3, 768])
def test_cosine_agreement_and_l2_identity(dimension):
    rng = np.random.default_rng(17)
    q, m = rng.normal(size=dimension), rng.normal(size=(13, dimension))
    score = get_metric("cosine_numpy").score(q, m)
    np.testing.assert_allclose(score, get_metric("cosine_math").score(q, m), atol=1e-12)
    q, m = q / np.linalg.norm(q), m / np.linalg.norm(m, axis=1, keepdims=True)
    np.testing.assert_allclose(get_metric("euclidean").score(q, m), 2 - 2 * score, atol=1e-12)


@pytest.mark.parametrize("name", ["cosine_math", "cosine_numpy", "euclidean", "dot"])
def test_known_scores_and_empty(name):
    metric = get_metric(name)
    expected = {
        "cosine_math": [1, 0, -1],
        "cosine_numpy": [1, 0, -1],
        "euclidean": [1, 2, 4],
        "dot": [2, 0, -1],
    }
    np.testing.assert_allclose(metric.score([1, 0], [[2, 0], [0, 1], [-1, 0]]), expected[name])
    assert metric.score([1, 0], []).shape == (0,)
    assert metric.higher_is_better == (name != "euclidean")
    with pytest.raises(ValueError):
        metric.score([1, 0], [[1]])
    with pytest.raises(ValueError):
        metric.score([1, float("nan")], [[1, 0]])
    with pytest.raises(ValueError):
        metric.score([], [])


@pytest.mark.parametrize("name", ["cosine_math", "cosine_numpy"])
def test_zero_rejected_and_extreme_finite_vectors_supported(name):
    metric = get_metric(name)
    with pytest.raises(ValueError, match="零向量"):
        metric.score([0, 0], [])
    with pytest.raises(ValueError, match="零向量"):
        metric.score([1, 0], [[0, 0]])
    np.testing.assert_allclose(metric.score([1e300, 0], [[1e-300, 0]]), [1])


def test_distance_dot_allow_zero_and_detect_overflow():
    assert get_metric("euclidean").score([0, 0], [[3, 4]]).tolist() == [25]
    assert get_metric("dot").score([0, 0], [[3, 4]]).tolist() == [0]
    with pytest.raises(ValueError, match="溢出"):
        get_metric("dot").score([1e308], [[1e308]])
    with pytest.raises(ValueError, match="未知"):
        get_metric("unknown")
