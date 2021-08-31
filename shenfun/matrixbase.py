r"""
This module contains classes for working with sparse matrices.

"""
from __future__ import division
from typing import List
from copy import copy, deepcopy
from collections.abc import Mapping, MutableMapping
from numbers import Number, Integral
import numpy as np
import sympy as sp
from scipy.sparse import bmat, dia_matrix, kron, diags as sp_diags
from scipy.sparse.linalg import spsolve
from mpi4py import MPI
from shenfun.config import config
from .utilities import integrate_sympy

__all__ = ['SparseMatrix', 'SpectralMatrix', 'extract_diagonal_matrix',
           'extract_bc_matrices', 'check_sanity', 'get_dense_matrix',
           'TPMatrix', 'BlockMatrix', 'BlockMatrices', 'Identity',
           'get_dense_matrix_sympy', 'get_dense_matrix_quadpy',
           'get_simplified_tpmatrices']

comm = MPI.COMM_WORLD

class SparseMatrix(MutableMapping):
    r"""Base class for sparse matrices.

    The data is stored as a dictionary, where keys and values are, respectively,
    the offsets and values of the diagonals. In addition, each matrix is stored
    with a coefficient that is used as a scalar multiple of the matrix.

    Parameters
    ----------
    d : dict
        Dictionary, where keys are the diagonal offsets and values the
        diagonals
    shape : two-tuple of ints
    scale : number, optional
        Scale matrix with this number

    Note
    ----
    The matrix format and storage is similar to Scipy's `dia_matrix`. The format is
    chosen because spectral matrices often are computed by hand and presented
    in the literature as banded matrices.
    Note that a SparseMatrix can easily be transformed to any of Scipy's formats
    using the `diags` method. However, Scipy's matrices are not implemented to
    act along different axes of multidimensional arrays, which is required
    for tensor product matrices, see :class:`.TPMatrix`. Hence the need for
    this SpectralMatrix class.

    Examples
    --------
    A tridiagonal matrix of shape N x N could be created as

    >>> from shenfun import SparseMatrix
    >>> import numpy as np
    >>> N = 4
    >>> d = {-1: 1, 0: -2, 1: 1}
    >>> S = SparseMatrix(d, (N, N))
    >>> dict(S)
    {-1: 1, 0: -2, 1: 1}

    In case of variable values, store the entire diagonal. For an N x N
    matrix use

    >>> d = {-1: np.ones(N-1),
    ...       0: -2*np.ones(N),
    ...       1: np.ones(N-1)}
    >>> S = SparseMatrix(d, (N, N))
    >>> dict(S)
    {-1: array([1., 1., 1.]), 0: array([-2., -2., -2., -2.]), 1: array([1., 1., 1.])}

    """
    # pylint: disable=redefined-builtin, missing-docstring

    def __init__(self, d, shape, scale=1.0):
        self._storage = dict(d)
        self.shape = shape
        self._diags = dia_matrix((1, 1))
        self.scale = scale
        self._matvec_methods = []
        self.solver = None

    def matvec(self, v, c, format=None, axis=0):
        """Matrix vector product

        Returns c = dot(self, v)

        Parameters
        ----------
        v : array
            Numpy input array of ndim>=1
        c : array
            Numpy output array of same shape as v
        format : str, optional
             Choice for computation

             - csr - Compressed sparse row format
             - dia - Sparse matrix with DIAgonal storage
             - python - Use numpy and vectorization
             - self - To be implemented in subclass
             - cython - Cython implementation that may be implemented in subclass
             - numba - Numba implementation that may be implemented in subclass

             Using config['matrix']['sparse']['matvec'] setting if format is None

        axis : int, optional
            The axis over which to take the matrix vector product

        """
        format = config['matrix']['sparse']['matvec'] if format is None else format
        N, M = self.shape
        c.fill(0)

        # Roll relevant axis to first
        if axis > 0:
            v = np.moveaxis(v, axis, 0)
            c = np.moveaxis(c, axis, 0)

        if format == 'python':
            for key, val in self.items():
                if np.ndim(val) > 0: # broadcasting
                    val = val[(slice(None), ) + (np.newaxis,)*(v.ndim-1)]
                if key < 0:
                    c[-key:min(N, M-key)] += val*v[:min(M, N+key)]
                else:
                    c[:min(N, M-key)] += val*v[key:min(M, N+key)]
            c *= self.scale

        else:
            diags = self.diags(format=format)
            P = int(np.prod(v.shape[1:]))
            y = diags.dot(v[:M].reshape(M, P)).squeeze()
            d = tuple([slice(0, m) for m in y.shape])
            c[d] = y.reshape(c[d].shape)

        if axis > 0:
            c = np.moveaxis(c, 0, axis)
            v = np.moveaxis(v, 0, axis)

        return c

    def diags(self, format=None, scaled=True):
        """Return a regular sparse matrix of specified format

        Parameters
        ----------
        format : str, optional
            Choice of matrix type (see scipy.sparse.diags)

            - dia - Sparse matrix with DIAgonal storage
            - csr - Compressed sparse row
            - csc - Compressed sparse column

            Using config['matrix']['sparse']['diags'] setting if format is None

        scaled : bool, optional
            Return matrix scaled by the constant self.scale if True

        Note
        ----
        This method returns the matrix scaled by self.scale if keyword scaled
        is True.

        """
        format = config['matrix']['sparse']['diags'] if format is None else format
        self._diags = sp_diags(list(self.values()), list(self.keys()),
                               shape=self.shape, format=format)
        scale = self.scale
        if isinstance(scale, np.ndarray):
            scale = np.atleast_1d(scale).item()
        return self._diags*scale if scaled else self._diags

    def __getitem__(self, key):
        v = self._storage[key]
        if hasattr(v, '__call__'):
            return v(key)
        return v

    def __delitem__(self, key):
        del self._storage[key]

    def __setitem__(self, key, val):
        self._storage[key] = val

    def __iter__(self):
        return iter(self._storage)

    def __len__(self):
        return len(self._storage)

    def __quasi__(self, Q):
        return Q.diags('csc')*self.diags('csc')

    def __eq__(self, a):
        if self.shape != a.shape:
            return False
        if not self.same_keys(a):
            return False
        d0 = self.diags('csr', False).data
        a0 = a.diags('csr', False).data
        if d0.shape[0] != a0.shape[0]:
            return False
        if not np.linalg.norm(d0-a0) < 1e-8:
            return False
        return True

    def __neq__(self, a):
        return not self.__eq__(a)

    def __imul__(self, y):
        """self.__imul__(y) <==> self*=y"""
        assert isinstance(y, Number)
        self.scale *= y
        return self

    def __mul__(self, y):
        """Returns copy of self.__mul__(y) <==> self*y"""
        if isinstance(y, Number):
            c = self.copy()
            c.scale *= y
            return c
        elif isinstance(y, np.ndarray):
            c = np.empty_like(y)
            c = self.matvec(y, c)
            return c
        elif isinstance(y, SparseMatrix):
            return self.diags('csc')*y.diags('csc')
        raise RuntimeError

    def __rmul__(self, y):
        """Returns copy of self.__rmul__(y) <==> y*self"""
        return self.__mul__(y)

    def __div__(self, y):
        """Returns elementwise division if `y` is a Number, or a linear algebra
        solve if `y` is an array.

        Parameters
        ----------
        y : Number or array

        """
        if isinstance(y, Number):
            assert abs(y) > 1e-8
            c = self.copy()
            c.scale /= y
            return c
        elif isinstance(y, np.ndarray):
            b = np.zeros_like(y)
            b = self.solve(y, b)
            return b
        else:
            raise NotImplementedError

    def __truediv__(self, y):
        """Returns copy self.__div__(y) <==> self/y"""
        return self.__div__(y)

    def __add__(self, d):
        """Return copy of self.__add__(y) <==> self+d"""

        if abs(self.scale) < 1e-15 and abs(d.scale) < 1e-15:
            f = SparseMatrix({0: 0}, self.shape)

        elif abs(self.scale) < 1e-15:
            f = SparseMatrix(deepcopy(dict(d)), d.shape, d.scale)

        elif abs(d.scale) < 1e-15:
            f = self.copy()

        else:
            assert isinstance(d, Mapping)
            f = SparseMatrix(deepcopy(dict(self)), self.shape, self.scale)
            f.incorporate_scale()
            d.incorporate_scale()
            for key, val in d.items():
                if key in f:
                    f[key] = f[key] + val
                else:
                    f[key] = val
        return f

    def __iadd__(self, d):
        """self.__iadd__(d) <==> self += d"""
        assert isinstance(d, Mapping)
        assert d.shape == self.shape

        if abs(d.scale) < 1e-16:
            return self

        elif abs(self.scale) < 1e-16:
            self.clear()
            for key, val in d.items():
                self[key] = val
            self.scale = d.scale
            return self

        self.incorporate_scale()
        d.incorporate_scale()
        for key, val in d.items():
            if key in self:
                self[key] = self[key] + val
            else:
                self[key] = val
        return self

    def __sub__(self, d):
        """Return copy of self.__sub__(d) <==> self-d"""
        assert isinstance(d, Mapping)

        if abs(self.scale) < 1e-15 and abs(d.scale) < 1e-15:
            f = SparseMatrix({0: 0}, self.shape)

        elif abs(self.scale) < 1e-15:
            f = SparseMatrix(deepcopy(dict(d)), d.shape, -d.scale)

        elif abs(d.scale) < 1e-15:
            f = self.copy()

        else:
            f = SparseMatrix(deepcopy(dict(self)), self.shape, self.scale)
            f.incorporate_scale()
            d.incorporate_scale()
            for key, val in d.items():
                if key in f:
                    f[key] = f[key] - val
                else:
                    f[key] = -val

        return f

    def __isub__(self, d):
        """self.__isub__(d) <==> self -= d"""
        assert isinstance(d, Mapping)
        assert d.shape == self.shape

        if abs(d.scale) < 1e-16:
            return self

        elif abs(self.scale) < 1e-16:
            self.clear()
            for key, val in d.items():
                self[key] = val
            self.scale = -d.scale
            return self

        self.incorporate_scale()
        d.incorporate_scale()
        for key, val in d.items():
            if key in self:
                self[key] = self[key] - val
            else:
                self[key] = -val
        return self

    def copy(self):
        """Return SparseMatrix deep copy of self"""
        return self.__deepcopy__()

    def __copy__(self):
        if self.__class__.__name__ == 'Identity':
            return self
        return SparseMatrix(copy(dict(self)), self.shape, self.scale)

    def __deepcopy__(self, memo=None, _nil=[]):
        if self.__class__.__name__ == 'Identity':
            return Identity(self.shape, self.scale)
        return SparseMatrix(deepcopy(dict(self)), self.shape, self.scale)

    def __neg__(self):
        """self.__neg__() <==> -self"""
        A = self.copy()
        A.scale = self.scale*-1
        return A

    def __hash__(self):
        return hash(frozenset(self))

    def get_key(self):
        return self.__hash__()

    def same_keys(self, a):
        return self.__hash__() == a.__hash__()

    def scale_array(self, c, sc):
        assert isinstance(sc, Number)
        if abs(sc-1) > 1e-8:
            c *= sc

    def incorporate_scale(self):
        if abs(self.scale-1) < 1e-8:
            return
        if hasattr(self, '_keyscale'):
            self._keyscale *= self.scale
        else:
            for key, val in self.items():
                self[key] = val*self.scale
        self.scale = 1

    def sorted_keys(self):
        return np.sort(np.array(list(self.keys())))

    def solve(self, b, u=None, axis=0, constraints=()):
        """Solve matrix system Au = b

        where A is the current matrix (self)

        Parameters
        ----------
        b : array
            Array of right hand side on entry and solution on exit unless
            u is provided.
        u : array, optional
            Output array
        axis : int, optional
            The axis over which to solve for if b and u are multi-
            dimensional
        constraints : tuple of 2-tuples
            The 2-tuples represent (row, val)
            The constraint indents the matrix row and sets b[row] = val

        Note
        ----
        Vectors may be one- or multidimensional.

        """
        if self.solver is None:
            self.solver = self.get_solver()(self)
        u = self.solver(b, u=u, axis=axis, constraints=constraints)
        return u

    def get_solver(self):
        """Return appropriate solver for self"""
        from .la import Solve, TDMA, TDMA_O, FDMA, TwoDMA, PDMA
        if len(self) == 2:
            if np.all(self.sorted_keys() == (0, 2)):
                return TwoDMA
        elif len(self) == 3:
            if np.all(self.sorted_keys() == (-2, 0, 2)):
                return TDMA
            elif np.all(self.sorted_keys() == (-1, 0, 1)) and self.issymmetric:
                return TDMA_O
        elif len(self) == 4:
            if np.all(self.sorted_keys() == (-2, 0, 2, 4)):
                return FDMA
        elif len(self) == 5 and self.issymmetric:
            if np.all(self.sorted_keys() == (-4, -2, 0, 2, 4)):
                return PDMA
        return Solve

    def isdiagonal(self):
        if len(self) == 1:
            if (0 in self):
                return True
        return False

    def isidentity(self):
        if not len(self) == 1:
            return False
        if (0 not in self):
            return False
        d = self[0]
        if np.all(d == 1):
            return True
        return False

    @property
    def issymmetric(self):
        #M = self.diags()
        #return (abs(M-M.T) > 1e-8).nnz == 0 # too expensive
        if np.sum(np.array(list(self.keys()))) != 0:
            return False
        for key, val in self.items():
            if key <= 0:
                continue
            if not np.all(abs(self[key]-self[-key]) < 1e-8):
                return False
        return True

    def clean_diagonals(self, reltol=1e-8):
        """Eliminate essentially zerovalued diagonals

        Parameters
        ----------
        reltol : number
            Relative tolerance
        """
        a = self * np.ones(self.shape[1])
        relmax = abs(a).max() / self.shape[1]
        if relmax == 0:
            relmax = 1
        list_keys = []
        for key, val in self.items():
            if abs(np.linalg.norm(val))/relmax < reltol:
                list_keys.append(key)
        for key in list_keys:
            del self[key]
        return self

    def is_bc_matrix(self):
        return False


class SpectralMatrix(SparseMatrix):
    r"""Base class for inner product matrices.

    Parameters
    ----------
    d : dict
        Dictionary, where keys are the diagonal offsets and values the
        diagonals
    trial : 2-tuple of (basis, int)
        The basis is an instance of a class for one of the bases in

        - :mod:`.legendre.bases`
        - :mod:`.chebyshev.bases`
        - :mod:`.fourier.bases`
        - :mod:`.laguerre.bases`
        - :mod:`.hermite.bases`
        - :mod:`.jacobi.bases`

        The int represents the number of times the trial function
        should be differentiated. Representing matrix column.
    test : 2-tuple of (basis, int)
        As trial, but representing matrix row.
    scale : number, optional
        Scale matrix with this number

    Examples
    --------

    Mass matrix for Chebyshev Dirichlet basis:

    .. math::

        (\phi_k, \phi_j)_w = \int_{-1}^{1} \phi_k(x) \phi_j(x) w(x) dx

    Stiffness matrix for Chebyshev Dirichlet basis:

    .. math::

        (\phi_k'', \phi_j)_w = \int_{-1}^{1} \phi_k''(x) \phi_j(x) w(x) dx

    The matrices can be automatically created using, e.g., for the mass
    matrix of the Dirichlet space::

    >>> from shenfun import FunctionSpace, SpectralMatrix
    >>> SD = FunctionSpace(16, 'C', bc=(0, 0))
    >>> M = SpectralMatrix({}, (SD, 0), (SD, 0))

    where the first (SD, 0) represents the test function and
    the second the trial function. The stiffness matrix can be obtained as::

    >>> A = SpectralMatrix({}, (SD, 0), (SD, 2))

    where (SD, 2) signals that we use the second derivative of this trial
    function.

    The automatically created matrices may be overloaded with more exactly
    computed diagonals.

    """
    def __init__(self, d, test, trial, scale=1.0, measure=1):
        assert isinstance(test[1], (int, np.integer))
        assert isinstance(trial[1], (int, np.integer))
        self.testfunction = test
        self.trialfunction = trial
        self.measure = measure
        shape = (test[0].dim(), trial[0].dim())
        if d == {}:
            if config['matrix']['sparse']['construct'] == 'dense':
                D = get_dense_matrix(test, trial, measure)[:shape[0], :shape[1]]
            elif config['matrix']['sparse']['construct'] == 'denser':
                D = get_denser_matrix(test, trial, measure)[:shape[0], :shape[1]]
            else:
                D = get_dense_matrix_sympy(test, trial, measure)[:shape[0], :shape[1]]
            d = extract_diagonal_matrix(D)
        SparseMatrix.__init__(self, d, shape, scale)

    def matvec(self, v, c, format=None, axis=0):
        u = self.trialfunction[0]
        ss = [slice(None)]*len(v.shape)
        ss[axis] = u.slice()
        c = super(SpectralMatrix, self).matvec(v[tuple(ss)], c, format=format, axis=axis)
        return c

    @property
    def tensorproductspace(self):
        """Return the :class:`.TensorProductSpace` this matrix has been
        computed for"""
        return self.testfunction[0].tensorproductspace

    @property
    def axis(self):
        """Return the axis of the :class:`.TensorProductSpace` this matrix is
        created for"""
        return self.testfunction[0].axis

    def __hash__(self):
        return hash(((self.testfunction[0].__class__, self.testfunction[1]),
                     (self.trialfunction[0].__class__, self.trialfunction[1])))

    def get_key(self):
        if self.__class__.__name__.endswith('mat'):
            return  self.__class__.__name__
        return self.__hash__()

    def simplify_diagonal_matrices(self):
        if self.isdiagonal():
            self.scale = self.scale*self[0]
            self[0] = 1

    def __eq__(self, a):
        if isinstance(a, Number):
            return False
        if not isinstance(a, SparseMatrix):
            return False
        if self.shape != a.shape:
            return False
        if self.get_key() != a.get_key():
            return False
        d0 = self.diags('csr', False).data
        a0 = a.diags('csr', False).data
        if d0.shape[0] != a0.shape[0]:
            return False
        if not np.linalg.norm(d0-a0) < 1e-8:
            return False
        return True

    def is_bc_matrix(self):
        return self.trialfunction[0].boundary_condition() == 'Apply'


class Identity(SparseMatrix):
    """The identity matrix in :class:`.SparseMatrix` form

    Parameters
    ----------
    shape : 2-tuple of ints
        The shape of the matrix
    scale : number, optional
        Scalar multiple of the matrix, defaults to unity

    """
    def __init__(self, shape, scale=1):
        SparseMatrix.__init__(self, {0:1}, shape, scale)
        self.measure = 1

    def solve(self, b, u=None, axis=0):
        if u is None:
            u = b
        else:
            assert u.shape == b.shape
            u[:] = b
        u *= (1/self.scale)
        return u

def BlockMatrices(tpmats):
    """Return two instances of the :class:`.BlockMatrix` class.

    Parameters
    ----------
    tpmats : sequence of :class:`.TPMatrix`'es or single :class:`.BlockMatrix`
        There can be both boundary matrices from inhomogeneous Dirichlet
        or Neumann conditions, as well as regular matrices.

    Note
    ----
    Use :class:`.BlockMatrix` directly if you do not have any inhomogeneous
    boundary conditions.
    """
    if isinstance(tpmats, BlockMatrix):
        tpmats = tpmats.get_mats()
    bc_mats = extract_bc_matrices([tpmats])
    assert len(bc_mats) > 0, 'No boundary matrices - use BlockMatrix'
    return BlockMatrix(tpmats), BlockMatrix(bc_mats)

class BlockMatrix:
    r"""A class for block matrices

    Parameters
    ----------
        tpmats : sequence of :class:`.TPMatrix` or :class:`.SparseMatrix`
            The individual blocks for the matrix

    Note
    ----
    The tensor product matrices may be either boundary
    matrices, regular matrices, or a mixture of both.

    Example
    -------
    Stokes equations, periodic in x and y-directions

    .. math::

        -\nabla^2 u - \nabla p &= 0 \\
        \nabla \cdot u &= 0 \\
        u(x, y, z=\pm 1) &= 0

    We use for the z-direction a Dirichlet basis (SD) and a regular basis with
    no boundary conditions (ST). This is combined with Fourier in the x- and
    y-directions (K0, K1), such that we get two TensorProductSpaces (TD, TT)
    that are tensor products of these bases

    .. math::

        TD &= K0 \otimes K1 \otimes SD \\
        TT &= K0 \otimes K1 \otimes ST

    We choose trialfunctions :math:`u \in [TD]^3` and :math:`p \in TT`, and then
    solve the weak problem

    .. math::

        \left( \nabla v, \nabla u\right) + \left(\nabla \cdot v, p \right) = 0\\
        \left( q, \nabla \cdot u\right) = 0

    for all :math:`v \in [TD]^3` and :math:`q \in TT`.

    To solve the problem we need to assemble a block matrix

    .. math::

        \begin{bmatrix}
            \left( \nabla v, \nabla u\right) & \left(\nabla \cdot v, p \right) \\
            \left( q, \nabla \cdot u\right) & 0
        \end{bmatrix}

    This matrix is assembled below

    >>> from shenfun import *
    >>> from mpi4py import MPI
    >>> comm = MPI.COMM_WORLD
    >>> N = (24, 24, 24)
    >>> K0 = FunctionSpace(N[0], 'Fourier', dtype='d')
    >>> K1 = FunctionSpace(N[1], 'Fourier', dtype='D')
    >>> SD = FunctionSpace(N[2], 'Legendre', bc=(0, 0))
    >>> ST = FunctionSpace(N[2], 'Legendre')
    >>> TD = TensorProductSpace(comm, (K0, K1, SD), axes=(2, 1, 0))
    >>> TT = TensorProductSpace(comm, (K0, K1, ST), axes=(2, 1, 0))
    >>> VT = VectorSpace(TD)
    >>> Q = CompositeSpace([VT, TD])
    >>> up = TrialFunction(Q)
    >>> vq = TestFunction(Q)
    >>> u, p = up
    >>> v, q = vq
    >>> A00 = inner(grad(v), grad(u))
    >>> A01 = inner(div(v), p)
    >>> A10 = inner(q, div(u))
    >>> M = BlockMatrix(A00+A01+A10)

    """
    def __init__(self, tpmats):
        assert isinstance(tpmats, (list, tuple))
        if isinstance(tpmats[0], TPMatrix):
            tpmats = get_simplified_tpmatrices(tpmats)
        tpmats = [tpmats] if not isinstance(tpmats[0], (list, tuple)) else tpmats
        self.testbase = testbase = tpmats[0][0].testbase
        self.trialbase = trialbase = tpmats[0][0].trialbase
        self.dims = dims = (testbase.num_components(), trialbase.num_components())
        self.mats = np.zeros(dims, dtype=int).tolist()
        self.solver = None
        self += tpmats

    def __add__(self, a):
        """Return copy of self.__add__(a) <==> self+a"""
        return BlockMatrix(self.get_mats()+a.get_mats())

    def __iadd__(self, a):
        """self.__iadd__(a) <==> self += a

        Parameters
        ----------
        a : :class:`.BlockMatrix` or list of :class:`.TPMatrix` instances

        """
        if isinstance(a, BlockMatrix):
            tpmats = a.get_mats()
        elif isinstance(a, (list, tuple)):
            tpmats = a
        for mat in tpmats:
            if not isinstance(mat, list):
                mat = [mat]
            for m in mat:
                assert isinstance(m, (TPMatrix, SparseMatrix))
                i, j = m.global_index
                m0 = self.mats[i][j]
                if isinstance(m0, int):
                    self.mats[i][j] = [m]
                else:
                    found = False
                    for n in m0:
                        if m == n:
                            n += m
                            found = True
                            continue
                    if not found:
                        self.mats[i][j].append(m)

    def get_mats(self, return_first=False):
        """Return flattened list of matrices in self"""
        tpmats = []
        for mi in self.mats:
            for mij in mi:
                if isinstance(mij, (list, tuple)):
                    for m in mij:
                        if isinstance(m, (TPMatrix, SparseMatrix)):
                            if return_first:
                                return m
                            else:
                                tpmats.append(m)
        return tpmats

    def matvec(self, v, c, format=None):
        """Compute matrix vector product

            c = self * v

        Parameters
        ----------
        v : :class:`.Function`
        c : :class:`.Function`

        Returns
        -------
        c : :class:`.Function`

        """
        assert v.function_space() == self.trialbase
        assert c.function_space() == self.testbase
        nvars = c.function_space().num_components()
        c = c.reshape(1, *c.shape) if nvars == 1 else c
        v = v.reshape(1, *v.shape) if nvars == 1 else v
        c.v.fill(0)
        z = np.zeros_like(c.v[0])
        for i, mi in enumerate(self.mats):
            for j, mij in enumerate(mi):
                if isinstance(mij, Number):
                    if abs(mij) > 1e-8:
                        c.v[i] += mij*v.v[j]
                else:
                    for m in mij:
                        z.fill(0)
                        z = m.matvec(v.v[j], z, format=format)
                        c.v[i] += z
        c = c.squeeze() if nvars == 1 else c
        v = v.squeeze() if nvars == 1 else v
        return c

    def __getitem__(self, ij):
        return self.mats[ij[0]][ij[1]]

    def get_offset(self, i, axis=0):
        return self.offset[i][axis]

    def contains_bc_matrix(self):
        """Return True if self contains a boundary TPMatrix"""
        for mi in self.mats:
            for mij in mi:
                if isinstance(mij, (list, tuple)):
                    for m in mij:
                        if m.is_bc_matrix() is True:
                            return True
        return False

    def contains_regular_matrix(self):
        for mi in self.mats:
            for mij in mi:
                if isinstance(mij, (list, tuple)):
                    for m in mij:
                        if m.is_bc_matrix() is False:
                            return True
        return False

    def diags(self, it=(0,), format=None):
        """Return global block matrix in scipy sparse format

        For multidimensional forms the returned matrix is constructed for
        given indices in the periodic directions.

        Parameters
        ----------
        it : n-tuple of ints
            where n is dimensions. These are the indices into the scale arrays
            of the TPMatrices in various blocks. Should be zero along the non-
            periodic direction.
        format : str
            The format of the returned matrix. See `Scipy sparse matrices <https://docs.scipy.org/doc/scipy/reference/sparse.html>`_

        """
        from .spectralbase import MixedFunctionSpace
        if self.contains_bc_matrix() and self.contains_regular_matrix():
            raise RuntimeError('diags only works for pure boundary or pure regular matrices. Consider splitting this BlockMatrix using :func:`.BlockMatrices`')
        bm = []
        for mi in self.mats:
            bm.append([])
            for mij in mi:
                if isinstance(mij, Number):
                    bm[-1].append(None)
                else:
                    m = mij[0]
                    if isinstance(self.testbase, MixedFunctionSpace):
                        d = m.diags(format)
                        for mj in mij[1:]:
                            d = d + mj.diags(format)
                    elif len(m.naxes) == 2: # 2 non-periodic directions
                        if len(m.mats) == 2:
                            d = m.scale.item()*kron(m.mats[0].diags(format), m.mats[1].diags(format))
                            for mj in mij[1:]:
                                d = d + mj.scale.item()*kron(mj.mats[0].diags(format), mj.mats[1].diags(format))
                        else:
                            iit = np.where(np.array(m.scale.shape) == 1, 0, it) # if shape is 1 use index 0, else use given index (shape=1 means the scale is constant in that direction)
                            d = m.scale[tuple(iit)]*kron(m.mats[m.naxes[0]].diags(format=format), m.mats[m.naxes[1]].diags(format=format))
                            for mj in mij[1:]:
                                iit = np.where(np.array(mj.scale.shape) == 1, 0, it)
                                sc = mj.scale[tuple(iit)]
                                d = d + sc*kron(mj.mats[mj.naxes[0]].diags(format=format), mj.mats[mj.naxes[1]].diags(format=format))

                    else:
                        iit = np.where(np.array(m.scale.shape) == 1, 0, it) # if shape is 1 use index 0, else use given index (shape=1 means the scale is constant in that direction)
                        sc = m.scale[tuple(iit)]
                        d = sc*m.mats[m.naxes[0]].diags(format)
                        for mj in mij[1:]:
                            iit = np.where(np.array(mj.scale.shape) == 1, 0, it)
                            sc = mj.scale[tuple(iit)]
                            d = d + sc*mj.mats[mj.naxes[0]].diags(format)
                    bm[-1].append(d)
        return bmat(bm, format=format)

    def solve(self, b, u=None, constraints=()):
        r"""
        Solve matrix system Au = b

        where A is the current :class:`.BlockMatrix` (self)

        Parameters
        ----------
        b : array
            Array of right hand side
        u : array, optional
            Output array
        constraints : sequence of 3-tuples of (int, int, number)
            Any 3-tuple describe a dof to be constrained. The first int
            represents the block number of the function to be constrained. The
            second int gives which degree of freedom to constrain and the number
            gives the value it should obtain. For example, for the global
            restriction that

            .. math::

                \frac{1}{V}\int p dx = number

            where we have

            .. math::

                p = \sum_{k=0}^{N-1} \hat{p}_k \phi_k

            it is sufficient to fix the first dof of p, \hat{p}_0, since
            the bases are created such that all basis functions except the
            first integrates to zero. So in this case the 3-tuple can be
            (2, 0, 0) if p is found in block 2 of the mixed basis.

            The constraint can only be applied to bases with no given
            explicit boundary condition, like the pure Chebyshev or Legendre
            bases.

        """
        from .la import BlockMatrixSolver
        sol = self.solver
        if self.solver is None:
            sol = BlockMatrixSolver(self)
            self.solver = sol
        u = sol(b, u, constraints)
        return u


class TPMatrix:
    """Tensor product matrix

    A :class:`.TensorProductSpace` is the tensor product of ``D`` univariate
    function spaces. A normal matrix (a second order tensor) is assembled from
    bilinear forms (i.e., forms containing both test and trial functions) on
    one univariate function space. A bilinear form on a tensor product space
    will assemble to ``D`` outer products of such univariate matrices. That is,
    for a two-dimensional tensor product you get fourth order tensors (outer
    product of two matrices), and three-dimensional tensor product spaces leads
    to a sixth order tensor (outer product of three matrices). This class
    contains ``D`` second order matrices. The complete matrix is as such the
    outer product of these ``D`` matrices.

    Note that the outer product of two matrices often is called the Kronecker
    product.

    Parameters
    ----------
    mats : sequence, or sequence of sequence of matrices
        Instances of :class:`.SpectralMatrix` or :class:`.SparseMatrix`
        The length of ``mats`` is the number of dimensions of the
        :class:`.TensorProductSpace`
    testspace : Function space
        The test :class:`.TensorProductSpace`
    trialspace : Function space
        The trial :class:`.TensorProductSpace`
    scale : array, optional
        Scalar multiple of matrices. Must have ndim equal to the number of
        dimensions in the :class:`.TensorProductSpace`, and the shape must be 1
        along any directions with a nondiagonal matrix.
    global_index : 2-tuple, optional
        Indices (test, trial) into mixed space :class:`.CompositeSpace`.
    testbase : :class:`.CompositeSpace`, optional
         Instance of the base test space
    trialbase : :class:`.CompositeSpace`, optional
         Instance of the base trial space
    """
    def __init__(self, mats, testspace, trialspace, scale=1.0, global_index=None,
                 testbase=None, trialbase=None):
        assert isinstance(mats, (list, tuple))
        assert len(mats) == len(testspace)
        self.mats = mats
        self.space = testspace
        self.trialspace = trialspace
        self.scale = scale
        self.pmat = 1
        self.naxes = testspace.get_nondiagonal_axes()
        self.global_index = global_index
        self.testbase = testbase
        self.trialbase = trialbase
        self._issimplified = False

    def get_simplified(self):
        """Return a version of self simplified by putting diagonal matrices in a
        scale array"""
        diagonal_axes = np.setxor1d(self.naxes, range(self.space.dimensions)).astype(int)
        if len(diagonal_axes) == 0 or self._issimplified:
            return self

        mats = []
        scale = copy(self.scale)
        for axis in range(self.dimensions):
            mat = self.mats[axis]
            if axis in diagonal_axes:
                d = mat[0]
                if np.ndim(d):
                    d = self.space[axis].broadcast_to_ndims(d*mat.scale)
                scale = scale*d
                mat = Identity(mat.shape)
            mats.append(mat)
        tpmat = TPMatrix(mats, self.space, self.trialspace, scale=scale,
                         global_index=self.global_index,
                         testbase=self.testbase, trialbase=self.trialbase)

        # Decomposition
        if len(self.space) > 1:
            s = tpmat.scale.shape
            ss = [slice(None)]*self.space.dimensions
            ls = self.space.local_slice()
            for axis, shape in enumerate(s):
                if shape > 1:
                    ss[axis] = ls[axis]
            tpmat.scale = (tpmat.scale[tuple(ss)]).copy()

        # If only one non-diagonal matrix, then make a simple link to
        # this matrix.
        if len(tpmat.naxes) == 1:
            tpmat.pmat = tpmat.mats[tpmat.naxes[0]]
        elif len(tpmat.naxes) == 2: # 2 nondiagonal
            tpmat.pmat = tpmat.mats
        tpmat._issimplified = True
        return tpmat

    def simplify_diagonal_matrices(self):
        if self._issimplified:
            return

        diagonal_axes = np.setxor1d(self.naxes, range(self.space.dimensions)).astype(int)
        if len(diagonal_axes) == 0:
            return

        for axis in diagonal_axes:
            mat = self.mats[axis]
            if self.dimensions == 1: # Don't bother with the 1D case
                continue
            else:
                d = mat[0]    # get diagonal
                if np.ndim(d):
                    d = self.space[axis].broadcast_to_ndims(d*mat.scale)
                self.scale = self.scale*d
                self.mats[axis] = Identity(mat.shape)

        # Decomposition
        if len(self.space) > 1:
            s = self.scale.shape
            ss = [slice(None)]*self.space.dimensions
            ls = self.space.local_slice()
            for axis, shape in enumerate(s):
                if shape > 1:
                    ss[axis] = ls[axis]
            self.scale = (self.scale[tuple(ss)]).copy()

        # If only one non-diagonal matrix, then make a simple link to
        # this matrix.
        if len(self.naxes) == 1:
            self.pmat = self.mats[self.naxes[0]]
        elif len(self.naxes) == 2: # 2 nondiagonal
            self.pmat = self.mats
        self._issimplified = True

    def solve(self, b, u=None, constraints=()):
        tpmat = self.get_simplified()
        if len(tpmat.naxes) == 0:
            sl = tuple([s.slice() for s in tpmat.trialspace.bases])
            d = tpmat.scale
            with np.errstate(divide='ignore'):
                d = 1./tpmat.scale
            if constraints:
                assert constraints[0] == (0, 0)
            # Constraint is enforced automatically
            d = np.where(np.isfinite(d), d, 0)
            if u is None:
                from .forms.arguments import Function
                u = Function(tpmat.space)
            u[sl] = b[sl] * d[sl]

        elif len(tpmat.naxes) == 1:
            from shenfun.la import SolverGeneric1ND
            H = SolverGeneric1ND([tpmat])
            u = H(b, u, constraints=constraints)

        elif len(tpmat.naxes) == 2:
            from shenfun.la import SolverGeneric2ND
            H = SolverGeneric2ND([tpmat])
            u = H(b, u, constraints=constraints)
        return u

    def matvec(self, v, c, format=None):
        tpmat = self.get_simplified()
        c.fill(0)
        if len(tpmat.naxes) == 0:
            c[:] = tpmat.scale*v
        elif len(tpmat.naxes) == 1:
            axis = tpmat.naxes[0]
            rank = v.rank if hasattr(v, 'rank') else 0
            if rank == 0:
                c = tpmat.pmat.matvec(v, c, format=format, axis=axis)
            else:
                c = tpmat.pmat.matvec(v[tpmat.global_index[1]], c, format=format, axis=axis)
            c[:] = c*tpmat.scale
        elif len(tpmat.naxes) == 2:
            # 2 non-periodic directions (may be non-aligned in second axis, hence transfers)
            npaxes = deepcopy(tpmat.naxes)
            space = tpmat.space
            newspace = False
            if space.forward.input_array.shape != space.forward.output_array.shape:
                space = space.get_unplanned(True) # in case self.space is padded
                newspace = True

            pencilA = space.forward.output_pencil
            subcomms = [s.Get_size() for s in pencilA.subcomm]
            axis = pencilA.axis
            assert subcomms[axis] == 1
            npaxes.remove(axis)
            second_axis = npaxes[0]
            pencilB = pencilA.pencil(second_axis)
            transAB = pencilA.transfer(pencilB, c.dtype.char)
            cB = np.zeros(transAB.subshapeB, dtype=c.dtype)
            cC = np.zeros(transAB.subshapeB, dtype=c.dtype)
            bb = tpmat.mats[axis]
            c = bb.matvec(v, c, format=format, axis=axis)
            # align in second non-periodic axis
            transAB.forward(c, cB)
            bb = tpmat.mats[second_axis]
            cC = bb.matvec(cB, cC, format=format, axis=second_axis)
            transAB.backward(cC, c)
            c *= tpmat.scale
            if newspace:
                space.destroy()

        return c

    def get_key(self):
        """Return key of the one nondiagonal matrix in the TPMatrix

        Note
        ----
        Raises an error of there are more than one single nondiagonal matrix
        in TPMatrix.
        """
        naxis = self.space.get_nondiagonal_axes()
        assert len(naxis) == 1
        return self.mats[naxis[0]].get_key()

    def isidentity(self):
        return np.all([m.isidentity() for m in self.mats])

    def isdiagonal(self):
        return np.all([m.isdiagonal() for m in self.mats])

    def is_bc_matrix(self):
        for m in self.mats:
            if m.is_bc_matrix():
                return True
        return False

    @property
    def dimensions(self):
        """Return dimension of TPMatrix"""
        return len(self.mats)

    def __mul__(self, a):
        """Returns copy of self.__mul__(a) <==> self*a"""
        if isinstance(a, Number):
            return TPMatrix(self.mats, self.space, self.trialspace, self.scale*a,
                            self.global_index, self.testbase, self.trialbase)

        elif isinstance(a, np.ndarray):
            c = np.empty_like(a)
            c = self.matvec(a, c)
            return c

    def __rmul__(self, a):
        """Returns copy of self.__rmul__(a) <==> a*self"""
        if isinstance(a, Number):
            return self.__mul__(a)
        else:
            raise NotImplementedError

    def __imul__(self, a):
        """Returns self.__imul__(a) <==> self*=a"""
        if isinstance(a, Number):
            self.scale *= a
        elif isinstance(a, np.ndarray):
            self.scale = self.scale*a
        return self

    def __div__(self, a):
        """Returns copy self.__div__(a) <==> self/a"""
        if isinstance(a, Number):
            return TPMatrix(self.mats, self.space, self.trialspace, self.scale/a,
                            self.global_index, self.testbase, self.trialbase)
        elif isinstance(a, np.ndarray):
            b = np.zeros_like(a)
            b = self.solve(a, b)
            return b
        else:
            raise NotImplementedError

    def __neg__(self):
        """self.__neg__() <==> -self"""
        A = self.copy()
        A.scale = self.scale*-1
        return A

    def __eq__(self, a):
        """Check if matrices and global_index are the same.

        Note
        ----
        The attribute scale may still be different
        """
        assert isinstance(a, TPMatrix)
        if not self.global_index == a.global_index:
            return False
        for m0, m1 in zip(self.mats, a.mats):
            if not m0.get_key() == m1.get_key():
                return False
            if not m0 == m1:
                return False
        return True

    def __ne__(self, a):
        return not self.__eq__(a)

    def __add__(self, a):
        """Return copy of self.__add__(a) <==> self+a"""
        assert isinstance(a, TPMatrix)
        assert self == a
        return TPMatrix(self.mats, self.space, self.trialspace, self.scale+a.scale,
                        self.global_index, self.testbase, self.trialbase)

    def __iadd__(self, a):
        """self.__iadd__(a) <==> self += a"""
        assert isinstance(a, TPMatrix)
        assert self == a
        self.scale = self.scale + a.scale
        return self

    def __sub__(self, a):
        """Return copy of self.__sub__(a) <==> self-a"""
        assert isinstance(a, TPMatrix)
        assert self == a
        return TPMatrix(self.mats, self.space, self.trialspace, self.scale-a.scale,
                        self.global_index, self.testbase, self.trialbase)

    def __isub__(self, a):
        """self.__isub__(a) <==> self -= a"""
        assert isinstance(a, TPMatrix)
        assert self == a
        self.scale = self.scale - a.scale
        return self

    def copy(self):
        """Return TPMatrix deep copy of self"""
        return self.__deepcopy__()

    def __copy__(self):
        mats = []
        for mat in self.mats:
            mats.append(mat.__copy__())
        return TPMatrix(mats, self.space, self.trialspace, self.scale,
                        self.global_index, self.testbase, self.trialbase)

    def __deepcopy__(self, memo=None, _nil=[]):
        mats = []
        for mat in self.mats:
            mats.append(mat.__deepcopy__())
        return TPMatrix(mats, self.space, self.trialspace, self.scale,
                        self.global_index, self.testbase, self.trialbase)

    def diags(self, format=None):
        assert self._issimplified is False
        if self.dimensions == 2:
            mat = kron(self.mats[0].diags(format=format),
                       self.mats[1].diags(format=format),
                       format=format)
        elif self.dimensions == 3:
            mat = kron(self.mats[0].diags(format=format),
                       kron(self.mats[1].diags(format=format),
                            self.mats[2].diags(format=format),
                            format=format),
                       format=format)
        elif self.dimensions == 4:
            mat = kron(self.mats[0].diags(format=format),
                       kron(self.mats[1].diags(format=format),
                            kron(self.mats[2].diags(format=format),
                                 self.mats[3].diags(format=format),
                                 format=format),
                            format=format),
                       format=format)
        elif self.dimensions == 5:
            mat = kron(self.mats[0].diags(format=format),
                       kron(self.mats[1].diags(format=format),
                            kron(self.mats[2].diags(format=format),
                                 kron(self.mats[3].diags(format=format),
                                      self.mats[4].diags(format=format),
                                      format=format),
                                 format=format),
                            format=format),
                       format=format)

        return mat*np.atleast_1d(self.scale).item()

def get_simplified_tpmatrices(tpmats : List[TPMatrix]) -> List[TPMatrix]:
    """Return copy of tpmats list, where diagonal matrices have been
    simplified and placed in scale arrays.

    Parameters
    ----------
    tpmats
        Instances of :class:`.TPMatrix`

    Returns
    -------
    List[TPMatrix]
        List of :class:`.TPMatrix`'es, that have been simplified

    """
    A = []
    for tpmat in tpmats:
        A.append(tpmat.get_simplified())

    # Add equal matrices
    B = [A[0]]
    for a in A[1:]:
        found = False
        for b in B:
            if a == b:
                b += a
                found = True
        if not found:
            B.append(a)
    return B


def check_sanity(A, test, trial, measure=1):
    """Sanity check for matrix.

    Test that automatically created matrix agrees with overloaded one

    Parameters
    ----------
    A : matrix
    test : 2-tuple of (basis, int)
        The basis is an instance of a class for one of the bases in

        - :mod:`.legendre.bases`
        - :mod:`.chebyshev.bases`
        - :mod:`.fourier.bases`
        - :mod:`.laguerre.bases`
        - :mod:`.hermite.bases`
        - :mod:`.jacobi.bases`

        The int represents the number of times the test function
        should be differentiated. Representing matrix row.
    trial : 2-tuple of (basis, int)
        As test, but representing matrix column.
    measure : sympy function of coordinate, optional
    """
    N, M = A.shape
    if measure == 1:
        D = get_dense_matrix(test, trial, measure)[:N, :M]
    else:
        D = get_denser_matrix(test, trial, measure)
    Dsp = extract_diagonal_matrix(D)
    for key, val in A.items():
        assert np.allclose(val*A.scale, Dsp[key])

def get_dense_matrix(test, trial, measure=1):
    """Return dense matrix automatically computed from basis

    Parameters
    ----------
    test : 2-tuple of (basis, int)
        The basis is an instance of a class for one of the bases in

        - :mod:`.legendre.bases`
        - :mod:`.chebyshev.bases`
        - :mod:`.fourier.bases`
        - :mod:`.laguerre.bases`
        - :mod:`.hermite.bases`
        - :mod:`.jacobi.bases`

        The int represents the number of times the test function
        should be differentiated. Representing matrix row.
    trial : 2-tuple of (basis, int)
        As test, but representing matrix column.
    measure : Sympy expression of coordinate, or number, optional
        Additional weight to integral. For example, in cylindrical
        coordinates an additional measure is the radius `r`.
    """
    K0 = test[0].slice().stop - test[0].slice().start
    K1 = trial[0].slice().stop - trial[0].slice().start
    N = test[0].N
    x = test[0].mpmath_points_and_weights(N, map_true_domain=False)[0]
    ws = test[0].get_measured_weights(N, measure)
    v = test[0].evaluate_basis_derivative_all(x=x, k=test[1])[:, :K0]
    u = trial[0].evaluate_basis_derivative_all(x=x, k=trial[1])[:, :K1]
    A = np.dot(np.conj(v.T)*ws[np.newaxis, :], u)
    if A.dtype.char in 'FDG':
        ni = np.linalg.norm(A.imag)
        if ni == 0:
            A = A.real.copy()
        elif np.linalg.norm(A.real) / ni > 1e14:
            A = A.real.copy()
    return A

def get_denser_matrix(test, trial, measure=1):
    """Return dense matrix automatically computed from basis

    Use slightly more quadrature points than usual N

    Parameters
    ----------
    test : 2-tuple of (basis, int)
        The basis is an instance of a class for one of the bases in

        - :mod:`.legendre.bases`
        - :mod:`.chebyshev.bases`
        - :mod:`.fourier.bases`
        - :mod:`.laguerre.bases`
        - :mod:`.hermite.bases`
        - :mod:`.jacobi.bases`

        The int represents the number of times the test function
        should be differentiated. Representing matrix row.
    trial : 2-tuple of (basis, int)
        As test, but representing matrix column.
    measure : Sympy expression of coordinate, or number, optional
        Additional weight to integral. For example, in cylindrical
        coordinates an additional measure is the radius `r`.
    """
    test2 = test[0].get_refined((test[0].N*3)//2)

    K0 = test[0].slice().stop - test[0].slice().start
    K1 = trial[0].slice().stop - trial[0].slice().start
    N = test2.N
    x = test2.mpmath_points_and_weights(N, map_true_domain=False)[0]
    ws = test2.get_measured_weights(N, measure)
    v = test[0].evaluate_basis_derivative_all(x=x, k=test[1])[:, :K0]
    u = trial[0].evaluate_basis_derivative_all(x=x, k=trial[1])[:, :K1]
    return np.dot(np.conj(v.T)*ws[np.newaxis, :], u)

def extract_diagonal_matrix(M, abstol=1e-10, reltol=1e-10):
    """Return SparseMatrix version of dense matrix ``M``

    Parameters
    ----------
    M : Numpy array of ndim=2
    abstol : float
        Tolerance. Only diagonals with max(:math:`|d|`) < tol are
        kept in the returned SparseMatrix, where :math:`d` is the
        diagonal
    reltol : float
        Relative tolerance. Only diagonals with
        max(:math:`|d|`)/max(:math:`|M|`) > reltol are kept in the
        returned SparseMatrix

    """
    d = {}
    relmax = abs(M).max()
    dtype = float if M.dtype == 'O' else M.dtype # For mpf object
    for i in range(M.shape[1]):
        u = M.diagonal(i).copy()
        if abs(u).max() > abstol and abs(u).max()/relmax > reltol:
            d[i] = np.array(u, dtype=dtype)

    for i in range(1, M.shape[0]):
        l = M.diagonal(-i).copy()
        if abs(l).max() > abstol and abs(l).max()/relmax > reltol:
            d[-i] = np.array(l, dtype=dtype)

    return SparseMatrix(d, M.shape)

def extract_bc_matrices(mats):
    """Extract boundary matrices from list of ``mats``

    Parameters
    ----------
    mats : list of list of instances of :class:`.TPMatrix` or
        :class:`.SparseMatrix`

    Returns
    -------
    list
        list of boundary matrices.

    Note
    ----
    The ``mats`` list is modified in place since boundary matrices are
    extracted.
    """
    bc_mats = []
    for a in mats:
        for b in a.copy():
            if b.is_bc_matrix():
                bc_mats.append(b)
                a.remove(b)
    return bc_mats

def get_dense_matrix_sympy(test, trial, measure=1):
    """Return dense matrix automatically computed from basis

    Parameters
    ----------
    test : 2-tuple of (basis, int)
        The basis is an instance of a class for one of the bases in

        - :mod:`.legendre.bases`
        - :mod:`.chebyshev.bases`
        - :mod:`.fourier.bases`
        - :mod:`.laguerre.bases`
        - :mod:`.hermite.bases`
        - :mod:`.jacobi.bases`

        The int represents the number of times the test function
        should be differentiated. Representing matrix row.
    trial : 2-tuple of (basis, int)
        As test, but representing matrix column.
    measure : Sympy expression of coordinate, or number, optional
        Additional weight to integral. For example, in cylindrical
        coordinates an additional measure is the radius `r`.
    """
    N = test[0].slice().stop - test[0].slice().start
    M = trial[0].slice().stop - trial[0].slice().start
    V = np.zeros((N, M), dtype=test[0].forward.output_array.dtype)
    x = sp.Symbol('x', real=True)

    if not measure == 1:
        if isinstance(measure, sp.Expr):
            s = measure.free_symbols
            assert len(s) == 1
            x = s.pop()
            xm = test[0].map_true_domain(x)
            measure = measure.subs(x, xm)
        else:
            assert isinstance(measure, Number)

    # Weight of weighted space
    measure *= test[0].weight()

    for i in range(test[0].slice().start, test[0].slice().stop):
        pi = np.conj(test[0].sympy_basis(i, x=x))
        for j in range(trial[0].slice().start, trial[0].slice().stop):
            pj = trial[0].sympy_basis(j, x=x)
            integrand = sp.simplify(measure*pi.diff(x, test[1])*pj.diff(x, trial[1]))
            V[i, j] = integrate_sympy(integrand,
                                      (x, test[0].sympy_reference_domain()[0], test[0].sympy_reference_domain()[1]))

    return V

def get_dense_matrix_quadpy(test, trial, measure=1):
    """Return dense matrix automatically computed from basis

    Using quadpy to compute the integral adaptively with high accuracy.
    This should be equivalent to integrating analytically with sympy,
    as long as the integrand is smooth enough and the integral can be
    found with quadrature.

    Parameters
    ----------
    test : 2-tuple of (basis, int)
        The basis is an instance of a class for one of the bases in

        - :mod:`.legendre.bases`
        - :mod:`.chebyshev.bases`
        - :mod:`.fourier.bases`
        - :mod:`.laguerre.bases`
        - :mod:`.hermite.bases`
        - :mod:`.jacobi.bases`

        The int represents the number of times the test function
        should be differentiated. Representing matrix row.
    trial : 2-tuple of (basis, int)
        As test, but representing matrix column.
    measure : Sympy expression of coordinate, or number, optional
        Additional weight to integral. For example, in cylindrical
        coordinates an additional measure is the radius `r`.
    """
    import quadpy
    N = test[0].slice().stop - test[0].slice().start
    M = trial[0].slice().stop - trial[0].slice().start
    V = np.zeros((N, M), dtype=test[0].forward.output_array.dtype)
    x = sp.Symbol('x', real=True)

    if not measure == 1:
        if isinstance(measure, sp.Expr):
            s = measure.free_symbols
            assert len(s) == 1
            x = s.pop()
            xm = test[0].map_true_domain(x)
            measure = measure.subs(x, xm)
        else:
            assert isinstance(measure, Number)

    # Weight of weighted space
    measure *= test[0].weight()

    for i in range(test[0].slice().start, test[0].slice().stop):
        pi = np.conj(test[0].sympy_basis(i, x=x))
        for j in range(trial[0].slice().start, trial[0].slice().stop):
            pj = trial[0].sympy_basis(j, x=x)
            integrand = sp.simplify(measure*pi.diff(x, test[1])*pj.diff(x, trial[1]))
            if not integrand == 0:
                V[i, j] = quadpy.c1.integrate_adaptive(sp.lambdify(x, integrand),
                                                       test[0].reference_domain())[0]

    return V
