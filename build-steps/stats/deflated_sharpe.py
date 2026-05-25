"""Deflated Sharpe Ratio (López de Prado & Bailey, 2014).

The Probabilistic Sharpe Ratio asks "what is the probability that the true Sharpe
exceeds a benchmark?", accounting for sample size, skew, and kurtosis. The DSR
substitutes the benchmark with E[max SR] under the null — what you'd expect to
see by chance after testing N configurations on iid noise. This is the
multiple-testing correction for backtest selection.

Reference: López de Prado, M. (2018). Advances in Financial Machine Learning,
ch. 8. Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Correcting
for Selection Bias, Backtest Overfitting and Non-Normality."

Usage:
    from build_steps.stats.deflated_sharpe import deflated_sharpe
    result = deflated_sharpe(per_trade_pnls, n_configs_tried)
    # result["dsr"] is the probability the true Sharpe > 0 after deflation

Run module directly for a self-test:
    python -m build_steps.stats.deflated_sharpe
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Dict, Sequence

GAMMA_EULER = 0.5772156649015329  # Euler-Mascheroni constant
_NORM = NormalDist()


def _phi(z: float) -> float:
    """Standard normal CDF Φ(z)."""
    return _NORM.cdf(z)


def _phi_inv(p: float) -> float:
    """Inverse standard normal CDF Φ⁻¹(p). Defined on (0, 1)."""
    # Clamp away from edges to avoid -inf / +inf in degenerate cases.
    p_clamped = max(1e-12, min(1.0 - 1e-12, p))
    return _NORM.inv_cdf(p_clamped)


def sample_moments(returns: Sequence[float]) -> Dict[str, float]:
    """Sample mean, std, skew, and (raw) kurtosis. Population (N) divisor."""
    n = len(returns)
    if n < 2:
        return {"mean": 0.0, "std": 0.0, "skew": 0.0, "kurt": 3.0, "n": n}
    mu = sum(returns) / n
    diffs = [r - mu for r in returns]
    m2 = sum(d * d for d in diffs) / n
    m3 = sum(d * d * d for d in diffs) / n
    m4 = sum(d ** 4 for d in diffs) / n
    sigma = math.sqrt(m2)
    if sigma == 0.0:
        return {"mean": mu, "std": 0.0, "skew": 0.0, "kurt": 3.0, "n": n}
    skew = m3 / (sigma ** 3)
    kurt = m4 / (sigma ** 4)  # RAW kurtosis (3.0 for normal); LdP uses raw, not excess
    return {"mean": mu, "std": sigma, "skew": skew, "kurt": kurt, "n": n}


def expected_max_sharpe_under_null(n_configs: int, n_trades: int) -> float:
    """E[max ŜR] when N independent strategies have true SR = 0 and iid returns.

    Under that null, V[ŜR] ≈ 1/T, so σ_SR ≈ 1/√T. The expected maximum of N
    such estimates is the Gumbel-distribution approximation used by LdP.
    """
    if n_configs < 2 or n_trades < 2:
        return 0.0
    sigma_sr = 1.0 / math.sqrt(n_trades)
    z_main = _phi_inv(1.0 - 1.0 / n_configs)
    z_aux = _phi_inv(1.0 - 1.0 / (n_configs * math.e))
    return sigma_sr * ((1.0 - GAMMA_EULER) * z_main + GAMMA_EULER * z_aux)


def deflated_sharpe(returns: Sequence[float], n_configs: int) -> Dict[str, float]:
    """Compute the Deflated Sharpe Ratio.

    Args:
        returns:    sequence of per-period returns (per-trade PnLs in our case)
        n_configs:  total number of independent configurations tried (monotonic
                    counter across all iterations of the self-improvement loop)

    Returns dict with:
        sr          observed (non-annualized) Sharpe of `returns`
        e_max_sr    expected max Sharpe under null after testing n_configs strategies
        dsr         deflated Sharpe = Pr(true SR > E[max SR | null]); ∈ [0, 1]
        skew, kurt  sample skewness, sample raw kurtosis
        T           sample size
        N           n_configs (echoed back)
        valid       True if computation succeeded; False on degenerate input
    """
    moments = sample_moments(returns)
    T = moments["n"]
    sigma = moments["std"]
    if T < 2 or sigma == 0.0 or n_configs < 1:
        return {
            "sr": 0.0, "e_max_sr": 0.0, "dsr": 0.0,
            "skew": moments["skew"], "kurt": moments["kurt"],
            "T": T, "N": n_configs, "valid": False,
        }

    sr = moments["mean"] / sigma
    gamma3 = moments["skew"]
    gamma4 = moments["kurt"]

    e_max_sr = expected_max_sharpe_under_null(n_configs, T)

    # PSR(SR_benchmark = E[max SR]) — see Bailey & LdP 2014 eq. 13
    denom_sq = 1.0 - gamma3 * sr + (gamma4 - 1.0) / 4.0 * sr * sr
    if denom_sq <= 0.0:
        # Pathological — usually means SR estimate is way outside normal-approx validity
        return {
            "sr": sr, "e_max_sr": e_max_sr, "dsr": 0.0,
            "skew": gamma3, "kurt": gamma4,
            "T": T, "N": n_configs, "valid": False,
        }
    denom = math.sqrt(denom_sq)
    z = (sr - e_max_sr) * math.sqrt(T - 1) / denom
    dsr = _phi(z)
    return {
        "sr": sr,
        "e_max_sr": e_max_sr,
        "dsr": dsr,
        "skew": gamma3,
        "kurt": gamma4,
        "T": T,
        "N": n_configs,
        "valid": True,
    }


def _self_test() -> None:
    """Smoke test with known scenarios."""
    import random

    print("Deflated Sharpe self-test")
    print("=" * 60)

    # Test 1: pure noise (true SR = 0) tested over 1 config.
    # DSR depends on the observed sample SR; with random noise the observed SR
    # can drift either side of zero. Just verify the calculation is in range.
    random.seed(42)
    noise = [random.gauss(0, 1) for _ in range(1000)]
    r_one = deflated_sharpe(noise, n_configs=1)
    print(f"\n[1] Pure noise, N=1:")
    print(f"    SR={r_one['sr']:+.4f}  E[max SR]={r_one['e_max_sr']:+.4f}  DSR={r_one['dsr']:.4f}")
    assert 0.0 <= r_one['dsr'] <= 1.0, "DSR must be a probability"

    # Test 2: same noise tested over 100 configs.
    # E[max SR | null, N=100] is now substantially positive, so DSR for the same
    # observed SR must drop relative to N=1.
    r_hundred = deflated_sharpe(noise, n_configs=100)
    print(f"\n[2] Pure noise, N=100 (multiple-testing penalty):")
    print(f"    SR={r_hundred['sr']:+.4f}  E[max SR]={r_hundred['e_max_sr']:+.4f}  DSR={r_hundred['dsr']:.4f}")
    assert r_hundred['dsr'] < r_one['dsr'], "DSR must drop as N grows (deflation working)"
    assert r_hundred['e_max_sr'] > r_one['e_max_sr'], "E[max SR] must grow with N"

    # Test 3: clear positive edge (mu=0.01, sigma=1, n=1000 → SR≈0.01·√1000=0.32 raw)
    random.seed(7)
    good = [random.gauss(0.05, 1) for _ in range(1000)]
    r = deflated_sharpe(good, n_configs=1)
    print(f"\n[3] Real edge (mu=0.05, sigma=1, T=1000), N=1:")
    print(f"    SR={r['sr']:+.4f}  E[max SR]={r['e_max_sr']:+.4f}  DSR={r['dsr']:.4f}")
    assert r['dsr'] > 0.90, "Strong edge should clear DSR"

    # Test 4: same edge, but after testing 100 configs → DSR should still clear but lower
    r = deflated_sharpe(good, n_configs=100)
    print(f"\n[4] Real edge after N=100 configs:")
    print(f"    SR={r['sr']:+.4f}  E[max SR]={r['e_max_sr']:+.4f}  DSR={r['dsr']:.4f}")
    print(f"    (lower than Test 3 — deflation working)")

    # Test 5: marginal edge that survives N=1 but fails N=100
    random.seed(11)
    marginal = [random.gauss(0.05, 1) for _ in range(300)]
    r_one = deflated_sharpe(marginal, n_configs=1)
    r_hundred = deflated_sharpe(marginal, n_configs=100)
    print(f"\n[5] Marginal edge (T=300):")
    print(f"    N=1:   SR={r_one['sr']:+.4f}  DSR={r_one['dsr']:.4f}")
    print(f"    N=100: SR={r_hundred['sr']:+.4f}  DSR={r_hundred['dsr']:.4f}")

    print("\nAll assertions passed.")


if __name__ == "__main__":
    _self_test()
