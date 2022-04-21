import functools
import sympy as sp
import numpy as np
from shenfun.matrixbase import SpectralMatrix
from . import bases

J  = bases.Orthogonal
CB = bases.CompositeBase
CD = bases.CompactDirichlet
CN = bases.CompactNeumann
UD = bases.UpperDirichlet
LD = bases.LowerDirichlet
P1 = bases.Phi1
P2 = bases.Phi2
P3 = bases.Phi3
P4 = bases.Phi4
BCG = bases.BCGeneric


class BJJmat(SpectralMatrix):
    r"""Mass matrix :math:`B=(b_{kj}) \in \mathbb{R}^{M \times N}`, where

    .. math::

        b_{kj}=(J_j, J_k)_w,

    :math:`J_k \in` :class:`.jacobi.bases.Orthogonal` and test and trial spaces have
    dimensions of M and N, respectively.

    """
    def assemble(self):
        test, trial = self.testfunction, self.trialfunction
        assert isinstance(test[0], J)
        assert isinstance(trial[0], J)
        from .recursions import h, n
        N = test[0].N
        k = np.arange(N, dtype=int)
        a = test[0].alpha
        b = test[0].beta
        hh = h(a, b, n, 0)
        d = {0: sp.lambdify(n, hh)(k)}
        return d

class _Jacmatrix(SpectralMatrix):
    def __init__(self, test, trial, scale=1, measure=1, assemble=None):
        SpectralMatrix.__init__(self, test, trial, scale=scale, measure=measure, assemble=assemble)


class _JacMatDict(dict):
    """Dictionary of inner product matrices

    Matrices that are missing keys are generated from Vandermonde type
    computations.

    """

    def __missing__(self, key):
        measure = 1 if len(key) == 2 else key[2]
        c = functools.partial(_Jacmatrix, measure=measure)
        self[key] = c
        return c

    def __getitem__(self, key):
        if len(key) == 3:
            matrix = functools.partial(dict.__getitem__(self, key),
                                       measure=key[2])
        else:
            matrix = dict.__getitem__(self, key)
        return matrix

mat = _JacMatDict({
    ((J,  0), (J,  0)): BJJmat,
})
