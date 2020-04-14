"""
Module for implementing helper functions.
"""
import types
try:
    from collections.abc import MutableMapping
except ImportError:
    from collections import MutableMapping
from collections import defaultdict
import numpy as np
import sympy as sp
from scipy.fftpack import dct
from shenfun.optimization import optimizer

__all__ = ['inheritdocstrings', 'dx', 'clenshaw_curtis1D', 'CachedArrayDict',
           'outer', 'apply_mask', 'integrate_sympy', 'get_scaling_factors',
           'get_covariant_basis', 'get_contravariant_basis', 'get_covariant_metric_tensor',
           'get_contravariant_metric_tensor']

def inheritdocstrings(cls):
    """Method used for inheriting docstrings from parent class

    Use as decorator::

         @inheritdocstrings
         class Child(Parent):

    and Child will use the same docstrings as parent even if
    a method is overloaded. The Child class may overload the
    docstring as well and a new docstring defined for a method
    in Child will overload the Parent.
    """
    for name, func in vars(cls).items():
        if isinstance(func, types.FunctionType) and not func.__doc__:
            for parent in cls.__bases__:
                parfunc = getattr(parent, name, None)
                if parfunc and getattr(parfunc, '__doc__', None):
                    func.__doc__ = parfunc.__doc__
                    break
    return cls

def dx(u):
    r"""Compute integral of u over domain

    .. math::

        \int_{\Omega} u dx

    Parameters
    ----------

        u : Array
            The Array to integrate

    """
    T = u.function_space()
    uc = u.copy()
    dim = len(u.shape)
    if dim == 1:
        w = T.points_and_weights(weighted=False)[1]
        return np.sum(uc*w).item()

    for ax in range(dim):
        uc = uc.redistribute(axis=ax)
        w = T.bases[ax].points_and_weights(weighted=False)[1]
        sl = [np.newaxis]*len(uc.shape)
        sl[ax] = slice(None)
        uu = np.sum(uc*w[tuple(sl)], axis=ax)
        sl = [slice(None)]*len(uc.shape)
        sl[ax] = np.newaxis
        uc[:] = uu[tuple(sl)]
    return uc.flat[0]

def clenshaw_curtis1D(u, quad="GC"):  # pragma: no cover
    """Clenshaw-Curtis integration in 1D"""
    assert u.ndim == 1
    N = u.shape[0]
    if quad == 'GL':
        w = np.arange(0, N, 1, dtype=float)
        w[2:] = 2./(1-w[2:]**2)
        w[0] = 1
        w[1::2] = 0
        ak = dct(u, 1)
        ak /= (N-1)
        return np.sqrt(np.sum(ak*w))

    assert quad == 'GC'
    d = np.zeros(N)
    k = 2*(1 + np.arange((N-1)//2))
    d[::2] = (2./N)/np.hstack((1., 1.-k*k))
    w = dct(d, type=3)
    return np.sqrt(np.sum(u*w))

class CachedArrayDict(MutableMapping):
    """Dictionary for caching Numpy arrays (work arrays)

    Example
    -------

    >>> import numpy as np
    >>> from shenfun.utilities import CachedArrayDict
    >>> work = CachedArrayDict()
    >>> a = np.ones((3, 4), dtype=int)
    >>> w = work[(a, 0, True)] # create work array with shape as a
    >>> print(w.shape)
    (3, 4)
    >>> print(w)
    [[0 0 0 0]
     [0 0 0 0]
     [0 0 0 0]]
    >>> w2 = work[(a, 1, True)] # Get different(note 1!) array of same shape/dtype
    """
    def __init__(self):
        self._data = {}

    def __getitem__(self, key):
        newkey, fill = self.__keytransform__(key)
        try:
            value = self._data[newkey]
        except KeyError:
            shape, dtype, _ = newkey
            value = np.zeros(shape, dtype=np.dtype(dtype, align=True))
            self._data[newkey] = value
        if fill:
            value.fill(0)
        return value

    @staticmethod
    def __keytransform__(key):
        assert len(key) == 3
        return (key[0].shape, key[0].dtype, key[1]), key[2]

    def __len__(self):
        return len(self._data)

    def __setitem__(self, key, value):
        self._data[self.__keytransform__(key)[0]] = value

    def __delitem__(self, key):
        del self._data[self.__keytransform__(key)[0]]

    def __iter__(self):
        return iter(self._data)

    def values(self):
        raise TypeError('Cached work arrays not iterable')

def outer(a, b, c):
    r"""Return outer product $c_{i,j} = a_i b_j$

    Parameters
    ----------
    a : Array of shape (N, ...)
    b : Array of shape (N, ...)
    c : Array of shape (N*N, ...)

    The outer product is taken over the first index of a and b,
    for all remaining indices.
    """
    av = a.v
    bv = b.v
    cv = c.v
    symmetric = a is b
    if av.shape[0] == 2:
        outer2D(av, bv, cv, symmetric)
    elif av.shape[0] == 3:
        outer3D(av, bv, cv, symmetric)
    return c

@optimizer
def outer2D(a, b, c, symmetric):
    c[0] = a[0]*b[0]
    c[1] = a[0]*b[1]
    if symmetric:
        c[2] = c[1]
    else:
        c[2] = a[1]*b[0]
    c[3] = a[1]*b[1]

@optimizer
def outer3D(a, b, c, symmetric):
    c[0] = a[0]*b[0]
    c[1] = a[0]*b[1]
    c[2] = a[0]*b[2]
    if symmetric:
        c[3] = c[1]
        c[6] = c[2]
        c[7] = c[5]
    else:
        c[3] = a[1]*b[0]
        c[6] = a[2]*b[0]
        c[7] = a[2]*b[1]
    c[4] = a[1]*b[1]
    c[5] = a[1]*b[2]
    c[8] = a[2]*b[2]

@optimizer
def apply_mask(u_hat, mask):
    if mask is not None:
        u_hat *= mask
    return u_hat

def integrate_sympy(f, d):
    """Exact definite integral using sympy

    Try to convert expression `f` to a polynomial before integrating.

    See sympy issue https://github.com/sympy/sympy/pull/18613 to why this is
    needed. Poly().integrate() is much faster than sympy.integrate() when applicable.

    Parameters
    ----------
    f : sympy expression
    d : 3-tuple
        First item the symbol, next two the lower and upper integration limits
    """
    try:
        p = sp.Poly(f, d[0]).integrate()
        return p(d[2]) - p(d[1])
    except sp.PolynomialError:
        return sp.integrate(f, d)

def get_cartesian_basis(N):
    e = np.zeros((N, N), dtype=object)
    for i in range(N):
        e[i, i] = sp.S(1)
    return e

def get_scaling_factors(psi, rv):
    b = get_covariant_basis(psi, rv)
    hi = np.zeros_like(psi)
    for i, s in enumerate(np.sum(b**2, axis=1)):
        hi[i] = sp.simplify(sp.sqrt(s))
    return hi

def get_covariant_basis(psi, rv):
    b = np.zeros((len(psi), len(rv)), dtype=object)
    for i, ti in enumerate(psi):
        for j, rj in enumerate(rv):
            b[i, j] = rj.diff(ti, 1)
    return b

def get_contravariant_basis(psi, rv):
    b = get_covariant_basis(psi, rv)
    bt = np.zeros_like(b)
    F = get_transform(b)
    bt = np.dot(F.T.inv(), np.eye(len(psi), dtype=int))
    for i in range(len(psi)):
        for j in range(len(psi)):
            bt[i, j] = sp.simplify(bt[i, j])
    return bt

def get_covariant_metric_tensor(psi, rv):
    b = get_covariant_basis(psi, rv)
    g = np.zeros((len(psi), len(psi)), dtype=object)
    for i in range(len(psi)):
        for j in range(len(psi)):
            g[i, j] = sp.simplify(np.dot(b[i], b[j]))
    return g

def get_contravariant_metric_tensor(g=None, psi=None, rv=None):
    if g is None:
        assert psi and rv
        g = get_covariant_metric_tensor(psi, rv)
    t = g.nonzero()
    gt = np.zeros_like(g)
    gt[t] = 1 / g[t]
    return gt

def get_transform(b):
    N = b.shape[0]
    F = sp.Matrix(np.zeros((N, N), object))
    for i in range(3):
        for j in range(3):
            F[i, j] = np.dot(b[i], np.eye(N, dtype=int)[j])
    return F

def get_christoffel_second(psi, rv):
    b = get_covariant_basis(psi, rv)
    bt = get_contravariant_basis(psi, rv)
    Ct = np.zeros((len(psi),)*len(psi), object)
    for i in range(len(psi)):
        for j in range(len(psi)):
            for k in range(len(psi)):
                Ct[i, j, k] = sp.simplify(np.dot(np.array([bij.diff(psi[j], 1) for bij in b[i]]), bt[k]))
    return Ct

def split(measures):
    def _split(mss, result):
        for ms in mss:
            ms = sp.sympify(ms)
            if isinstance(ms, sp.Mul):
                # Multiplication of two or more terms
                _split(ms.args, result)
                continue

            # Something else with only one symbol
            sym = ms.free_symbols
            assert len(sym) <= 1
            if len(sym) == 1:
                sym = sym.pop()
                result[str(sym)] *= ms
            else:
                result['x'] *= ms
    result = defaultdict(lambda: 1)
    _split(measures, result)
    return result
