r"""
Solve Poisson equation in 1D with the tau method

.. math::

    \nabla^2 u = f,

The equation to solve is

.. math::

     (\nabla^2 u, v) = (f, v)

with Dirichlet boundary conditions u(-1) = a and u(1) = b.

"""
import sys
import sympy as sp
import numpy as np
import scipy.sparse as scp
from shenfun import *

assert len(sys.argv) == 3, 'Call with two command-line arguments'
assert sys.argv[-1].lower() in ('legendre', 'chebyshev')
assert isinstance(int(sys.argv[-2]), int)

# Get family from args
family = sys.argv[-1].lower()

# Size of discretization
N = int(sys.argv[-2])

T = FunctionSpace(N, family=family, domain=(-1, 2))
u = TrialFunction(T)
v = TestFunction(T)

# Use sympy to compute a rhs, given an analytical solution
x = sp.symbols("x", real=True)
x_map = T.map_reference_domain(x)
ue = sp.cos(4*sp.pi*x_map)
fe = ue.diff(x, 2)

# Get f on quad points
fj = Array(T, buffer=fe)

# Compute right hand side of Poisson equation
f_hat = Function(T)
f_hat = inner(v, fj, output_array=f_hat)

# Get left hand side of Poisson equation
A = inner(v, div(grad(u)))

# Fix boundary conditions
A = A.diags('lil')
A[-2] = (-1)**np.arange(N)
A[-1] = np.ones(N)
A = A.tocsc()

# Fix right hand side boundary conditions
f_hat[-2] = ue.subs(x, T.domain[0])
f_hat[-1] = ue.subs(x, T.domain[1])

# Solve
u_hat = Function(T)
u_hat[:] = scp.linalg.spsolve(A, f_hat)
uj = u_hat.backward()

# Compare with analytical solution
ua = Array(T, buffer=ue)
print("Error=%2.16e" %(np.sqrt(dx((uj-ua)**2))))
assert np.allclose(uj, ua)