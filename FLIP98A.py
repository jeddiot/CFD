# This code adopts $\dot{p}/m + \overline{\boldsymbol{nabla} \cdot \boldsymbol{v}} = 0$ as the state eqn.

import numpy as np
import taichi as ti
import os as os
import sys as sys
import time
from pyevtk.hl import pointsToVTK
import colorama
import math

def format_exp(x, n, d=6):
    significand = x / 10 ** n
    exp_sign = '+' if n >= 0 else ''
    # exp_sign = '+' if n >= 0 else '-'
    return f'{significand:.{d}f}e{exp_sign}{n:02d}'

time0 = time.time()
ti.init(arch=ti.gpu, device_memory_GB = 3.0)

#-----------switches-----------#
switch_vt_I_APIC = False # True: velocity APIC (Affine particle-in-cell); false: velocity PIC (particle-in-cell)
# switch_pSolver = True # True: linear pressure field; false: constant pressure field.
switch_overlineF = False # F-Bar pressure stabilization
switch_overlinedivv = False # True: use $\boldsymbol{\nabla} \cdot \boldsymbol{v}_0$; false: use $\boldsymbol{\nabla} \cdot \boldsymbol{v}_p$
switch_penaltyEBC = False
switch_getRK_Bspline = True # True: B-spline kernel, False: Tent kernel
# switch_US = False # True: USF, False: USL

visc = 1e-3 # [Pa s], 0.001
nu = 4.999e-1 # 4.999e-1 # [unitless], Poisson's ratio
K = 2e9 # E/2/(1 - nu) # [Pa], bulk modulus
E = K * 2 * (1 - nu) # 2e9 + 4e5 # [Pa] Young's modulus
G = K * (1 - nu)/(1 + nu) # [Pa] Shear modulus
# mu = E / (2 * (1 + nu))
# lambd = E * nu / ((1 + nu) * (1 - 2 * nu))
rho = 997.5 # [kg m^{-3}], Particle density
gravity = -9.81 # [m s^{-2}], Gravity (should be negative) 

#-----------numericalParameter-----------#
dim = 2 # Dimension of the problem
epsilon = 1e-15 # Numerical tolerance
simTime = 0.1 # 2.0 # Total simulation time
timeTotal = 0e-15 # Initialize the simulation time at 0
dt = 1e-5 # 1e-6, 5e-7 work fine for num_g == 100+4 # Timestep
betaNor = 1e6 # penalty
mixRatio = 1 # 0: point-wise pressure, 1: mixed formulation pressure
sizeScale = 1.0
eta = 0.0 # 0.98 # FLIP blend parameter: 0.0 = full APIC; 1.0 = full FLIP

filepath = "mov"
vtkpath  = "vtk"
if mixRatio==1:
    filepath = filepath + "_mixed"
    vtkpath  = vtkpath + "_mixed"
elif mixRatio==0:
    filepath = filepath + "_pointwise"
    vtkpath  = vtkpath + "_pointwise"
filepath = filepath + "_dt" + format_exp(dt, math.floor(math.log10(dt)), 0)
vtkpath  = vtkpath + "_dt" + format_exp(dt, math.floor(math.log10(dt)), 0)
if switch_penaltyEBC:
    filepath = filepath + "_betaNor" + format_exp(betaNor, math.floor(math.log10(betaNor)), 0)
    vtkpath  = vtkpath + "_betaNor" + format_exp(betaNor, math.floor(math.log10(betaNor)), 0)
# if switch_pSolver:
#     filepath = "mov_v1pdot1_dt1e-7_betaNor1e6_divvBar_tif64"
#     vtkpath  = "vtk_v1pdot1_dt1e-7_betaNor1e6_divvBar_tif64"
# else:
#     filepath = "mov_v1pdot0"
#     vtkpath  = "vtk_v1pdot0"
if switch_overlinedivv:
    filepath = filepath + "_divvBar"
    vtkpath  = vtkpath + "_divvBar"

# filepath = filepath + "_FLIP098APIC002"
# vtkpath  = vtkpath + "_FLIP098APIC002"

np_x = 80
np_y = 40
num_p = np_x * np_y

len_domain = 6.0/sizeScale # [m]
W_fluid = 4.0/sizeScale # [m] # Width of Liquid square (true dimension)
H_fluid = 2.0/sizeScale # [m] # Height of Liquid square (true dimension)
volume0_p = (W_fluid * H_fluid) / num_p

num_g = int(71) # int(25) # int(71) # 
num_g += int(4) # num_g+2+2 by num_g+2+2 including the boundaries
num_cell = int(num_g - 1) 
dx = len_domain  / float(num_cell - 4) # len_domain / float(num_g - 1)
inv_dx = 1 / dx

aNorm = 1.5 # RK normalized Kernel function support size
a = aNorm * dx # RK Kernel function support size
nodeNum = int(a*inv_dx*2 + epsilon) # (max) Number of 1D grid nodes in the support of each particle
shift = float(a*inv_dx - 1.0) # set as 0.5, used to find "base"

fb = ti.Vector.field(2, dtype=ti.f64, shape=()) # gravity
fb[None] = [0, W_fluid*H_fluid*rho*gravity] # [m kg s^{-2}], Set initial gravity direction to -y

beta = betaNor * rho * dx**2 # volume0_p # 1e30 # Penalty parameter on EBC
# tau = dx**2 * E / G # PSPG parameter

xt_p = ti.Vector.field(2, dtype=ti.f64, shape=num_p) # Particle position
vt_p = ti.Vector.field(2, dtype=ti.f64, shape=num_p) # Particle velocity
Lt_p = ti.Matrix.field(2, 2, dtype=ti.f64, shape=num_p) # Velocity gradient (APIC)
Ft_p = ti.Matrix.field(2, 2, dtype=ti.f64, shape=num_p) # Deformation gradient
sigma = ti.Matrix.field(2, 2, dtype=ti.f64, shape=num_p) # Particle stress

material = ti.field(dtype=int, shape=num_p) # Material id
volumet_p = ti.field(dtype=ti.f64, shape=num_p) # Particle volume
mt_p = ti.field(dtype=ti.f64, shape=num_p)

vt_I = ti.Vector.field(2, dtype=ti.f64, shape=(num_g, num_g)) # Grid node momentum / velocity
vt_IInitial = ti.Vector.field(2, dtype=ti.f64,shape=(num_g, num_g)) # Grid node initial momentum / velocity (for each timestep, FLIP)
mt_I = ti.Matrix.field(2, 2, dtype=ti.f64, shape=(num_g, num_g)) 
volumet_I = ti.field(dtype=ti.f64, shape=(num_g, num_g))
pt_I = ti.field(dtype=ti.f64, shape=(num_g, num_g))

volume0_0 = ti.field(dtype=ti.f64, shape=(num_g - 1, num_g - 1)) # Pressure stabilization (F-bar)
volumet_0 = ti.field(dtype=ti.f64, shape=(num_g - 1, num_g - 1)) # Pressure stabilization (F-bar)
cell = ti.field(dtype=ti.f64, shape=(num_g - 1, num_g - 1)) # Pressure stabilization (F-bar)

detF = ti.field(dtype=ti.f64, shape=num_p) # determinant of F (deformation gradient)

PartitionOfUnity = ti.field(dtype=ti.f64, shape=num_p) # Check for each particle (POU)
Cons = ti.field(dtype=ti.f64, shape=num_p) # Check for each particle (consistency)
Cons_dx = ti.field(dtype=ti.f64, shape=num_p) # Check for each particle (gradient consistency)
Cons_dy = ti.field(dtype=ti.f64, shape=num_p) # Check for each particle (gradient consistency)

pt_p = ti.field(dtype=ti.f64, shape=num_p)
divvt_p = ti.field(dtype=ti.f64, shape=num_p) # \boldsymbol{nabla} \cdot \boldsymbol{v}_p^{t}
rho_p = ti.field(dtype=ti.f64, shape=num_p)

# -----------$div(\boldsymbol{v}_p^t)$-projection method
divvt_0_numerator = ti.field(dtype=ti.f64, shape=(num_g, num_g))
divvt_0_denominator = ti.field(dtype=ti.f64, shape=(num_g, num_g))
divvt_0 = ti.field(dtype=ti.f64, shape=(num_g, num_g))

# --------------------penaltyMethod
x_L_left = ti.Vector.field(2, dtype=ti.f64, shape=num_cell-4)
x_L_right = ti.Vector.field(2, dtype=ti.f64, shape=num_cell-4)
x_L_bot = ti.Vector.field(2, dtype=ti.f64, shape=num_cell-4)
x_L_top = ti.Vector.field(2, dtype=ti.f64, shape=num_cell-4)

@ti.func 
def getRK(xp, base, a): 
    phiMat = ti.Matrix.zero(ti.f64,3,3)                  # Initialize the matrix of phi values for each surrounding grid node at the current particle location
    dphiXMat = ti.Matrix.zero(ti.f64,3,3)                # Initialize the matrix of dphi/dx values for each surrounding grid node at the current particle location
    dphiYMat = ti.Matrix.zero(ti.f64,3,3)                # Initialize the matrix of dphi/dy values for each surrounding grid node at the current particle location
    M = ti.Matrix.zero(ti.f64,3,3)                       # Initialize the moment matrix 
    w = ti.Matrix.zero(ti.f64, nodeNum, dim)             # Initialize the kernel function vector for weights at each grid node for each coordinate direction (1D)
    weight = ti.Matrix.zero(ti.f64, nodeNum, nodeNum)    # Kernel weights in 2D 
    for i, d in ti.static(ti.ndrange(nodeNum, dim)):
        gridNode = float( i + base[d] ) * dx            # Current grid node location
        if gridNode >= 0:
            z = abs( xp[d] - float(gridNode)) / a       # Normalized distance from particle to grid node
            if switch_getRK_Bspline:
                if 0 <= z and z < 0.5:                      # Cubic b-spline kernel functions
                    w[i,d] = 2/3 - 4*z**2 + 4*z**3 
                elif 1/2 <= z and z < 1:
                    w[i,d] = 4/3 - 4*z + 4*z**2 - (4/3)*z**3
            else:
                if 0 <= z and z < 1:
                    w[i,d] = z - 1
                elif 1 <= z: 
                    w[i,d] = 0 
    for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)): 
        gridNode = [float(i+base[0]) * dx, float(j+base[1]) * dx] # Current grid node location
        if gridNode[0] >= 0 and gridNode[1] >= 0: 
            weight[i,j] = w[i,0] * w[j,1] # Define kernel function weights in 2D
            Pxi = (ti.Vector([1.0, xp[0] - gridNode[0], xp[1] - gridNode[1]])) # Define P(xi - xp)
            if weight[i,j] != 0:
                M += weight[i,j] * Pxi @ Pxi.transpose() # Define the moment matrix        
    M_inv = M.inverse()
    for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)): # Loop over neighboring grid nodes                    
        gridNode = [float(i+base[0]) * dx, float(j+base[1]) * dx] # Current grid node location
        if weight[i,j] != 0:
            if gridNode[0] >= 0 and gridNode[1] >= 0:
                Pxi = ti.Vector([1.0, xp[0] - gridNode[0], xp[1] - gridNode[1]])  # Define P(xi - xp)
                Pxp = ti.Vector([1.0, xp[0] - xp[0], xp[1] - xp[1]])
                dPxpX = ti.Vector([0.0, -1.0, 0.0])
                dPxpY = ti.Vector([0.0, 0.0, -1.0])

                phi = weight[i,j] * (Pxp.transpose() @ M_inv @ Pxi)                   # Define phi
                dphi_x1 = weight[i,j] * (dPxpX.transpose() @ M_inv @ Pxi)             # Define dphi/dx
                dphi_x2 = weight[i,j] * (dPxpY.transpose() @ M_inv @ Pxi)             # Define dphi/dy
                
                phiMat[i,j] = phi[0]
                dphiXMat[i,j] = dphi_x1[0]
                dphiYMat[i,j] = dphi_x2[0]

    return phiMat, dphiXMat, dphiYMat

@ti.kernel
def substep():
    for i, j in mt_I: # 归零，这很重要，不然计算会出错，有些数字会加到爆炸
        vt_I[i, j] = [0,0] # $v^{t+\Delta t}_I$
        vt_IInitial[i, j] = [0,0] # $v^t_I$
        mt_I[i, j] = [[0, 0],[0, 0]]
        volumet_I[i, j] = 0
        pt_I[i, j] = 0

        # volume0_0[i,j] = 0
        # volumet_0[i,j] = 0
        # cell[i,j] = 0
        divvt_0_denominator[i, j] = 0
        divvt_0_numerator[i, j] = 0
        divvt_0[i, j] = 0

    if switch_penaltyEBC:
        for k in x_L_left:
            base = (x_L_left[k] * inv_dx - shift).cast(int)
            Psi_I, notused0, notused1 = getRK(x_L_left[k], base, a)
            s1 = 1
            s2 = 0
            S = ti.Matrix([[s1, 0], [0, s2]])
            for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)):
                offset = ti.Vector([i, j])
                if float(base[0] + offset[0]) >= 2: # <= 2:
                    mt_I[base + offset] += dx * beta * Psi_I[i,j] * S
                if float(base[0] + offset[0]) < 2: # >= num_cell - 2:
                    mt_I[base + offset] += dx * beta * S

        for k in x_L_right:
            base = (x_L_right[k] * inv_dx - shift).cast(int)
            Psi_I, notused0, notused1 = getRK(x_L_right[k], base, a)
            s1 = 1
            s2 = 0
            S = ti.Matrix([[s1, 0], [0, s2]])
            for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)):
                offset = ti.Vector([i, j])
                if float(base[0] + offset[0]) <= num_cell - 2: # >= num_cell - 2:
                    mt_I[base + offset] += dx * beta * Psi_I[i,j] * S
                if float(base[0] + offset[0]) > num_cell - 2: # >= num_cell - 2:
                    mt_I[base + offset] += dx * beta * S

        for k in x_L_bot:
            base = (x_L_bot[k] * inv_dx - shift).cast(int)
            Psi_I, notused0, notused1 = getRK(x_L_bot[k], base, a)
            s1 = 0
            s2 = 1
            S = ti.Matrix([[s1, 0], [0, s2]])
            for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)):
                offset = ti.Vector([i, j])
                if float(base[1] + offset[1]) >= 2: # <= 2:
                    mt_I[base + offset] += dx * beta * Psi_I[i,j] * S
                if float(base[1] + offset[1]) < 2: # >= num_cell - 2:
                    mt_I[base + offset] += dx * beta * S

        for k in x_L_top:
            base = (x_L_top[k] * inv_dx - shift).cast(int)
            Psi_I, notused0, notused1 = getRK(x_L_top[k], base, a)
            s1 = 0
            s2 = 1
            S = ti.Matrix([[s1, 0], [0, s2]])
            for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)):
                offset = ti.Vector([i, j])
                if float(base[1] + offset[1]) <= num_cell - 2: # >= num_cell - 2:
                    mt_I[base + offset] += dx * beta * Psi_I[i,j] * S
                if float(base[1] + offset[1]) > num_cell - 2: # >= num_cell - 2:
                    mt_I[base + offset] += dx * beta * S

    for p in xt_p: 
        cellBase = (xt_p[p] * inv_dx).cast(int)
        base = (xt_p[p] * inv_dx - shift).cast(int) # Define the bottom left corner of the surrounding 3x3 grid of neighboring nodes
        fx = (xt_p[p] * inv_dx - base.cast(ti.f64)) * dx # Define the vector from "base" to the current particle
        
        # overlineF = ( (cell[cellBase[0], cellBase[1]] / detF[p]) ** (1/2) ) * F[p]
        # if switch_overlineF:
        #     F[p] = overlineF
        # _, sig_FBar, _ = ti.svd(F[p])
        # J_bar = 1.0
        # for d in ti.static(range(dim)):
        #     J_bar *= sig_FBar[d, d]

        # volumet_p[p] = volume0_p # update particle volumes using F-Bar
        # mt_p[p] = volumet_p[p] * rho # update particle masses using F-Bar

        # Ft_p[p] = (ti.Matrix.identity(ti.f64, 2) + dt * Lt_p[p]) @ Ft_p[p]

        # if switch_US:
        # sigma[p] = - pt_p[p] * ti.Matrix.identity(ti.f64, 2) + visc * (Lt_p[p] + Lt_p[p].transpose()) # Fluid model

        PartitionOfUnity[p] = ti.cast(0, ti.f64) # reset PoU, consistency, and gradient consistency for each particle
        Cons[p] = ti.cast(0, ti.f64)
        Cons_dx[p] = ti.cast(0, ti.f64)
        Cons_dy[p] = ti.cast(0, ti.f64)

        Psi_I, Psi_Icommax, Psi_Icommay = getRK(xt_p[p], base, a)

        for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)): # for I \in 3 by 3 grid
            gridNode = [float(i+base[0]) * dx, float(j+base[1]) * dx] # Current grid node location
            B_I = ti.Vector([Psi_Icommax[i,j], Psi_Icommay[i,j]]) # Assemble a phi gradient vector
            PartitionOfUnity[p] += Psi_I[i,j] # POU
            Cons[p] += Psi_I[i,j] * gridNode[0] * gridNode[1] # Consistency
            Cons_dx[p] += B_I[0] * gridNode[0] * gridNode[1] # Gradient consistency
            Cons_dy[p] += B_I[1] * gridNode[0] * gridNode[1] # Gradient consistency

            offset = ti.Vector([i, j]) # Vector of grid node positions relative to "base" 
            dpos = offset.cast(ti.f64)*dx - fx # A vector from the current grid node to the current particle

            vt_I_APIC = Lt_p[p] @ dpos # define the contribution of the velocity gradient to the particle momentum 

            volumet_I[base + offset] += Psi_I[i,j] * volumet_p[p]
            mt_I[base + offset] += Psi_I[i,j] * mt_p[p] * ti.Matrix.identity(ti.f64, 2)

            # get v_I^{t}
            if switch_vt_I_APIC: # APIC v_I^{t}
                vt_IInitial[base + offset] += Psi_I[i,j] * mt_p[p] * ( vt_p[p] + vt_I_APIC )
                vt_I[base + offset] += Psi_I[i,j] * mt_p[p] * ( vt_p[p] + vt_I_APIC )
            else: # PIC v_I^{t}
                vt_IInitial[base + offset] += Psi_I[i,j] * mt_p[p] * vt_p[p]
                vt_I[base + offset] += Psi_I[i,j] * mt_p[p] * vt_p[p] # obtain $(mv)^t_I$
            # get v_I^{t + \Delta t}
            vt_I[base + offset] += dt * volumet_p[p] * Psi_I[i,j] * fb[None] # add external force $\Delta t \boldsymbol{F}_I^{int}$ to $(mv)^t_I$
            vt_I[base + offset] -= dt * volumet_p[p] * ( sigma[p] @ B_I ) # add internal force $\Delta t \boldsymbol{F}_I^{ext}$ to $(mv)^t_I$

            # get PIC p_I^{t}
            pt_I[base + offset] += Psi_I[i,j]*volumet_p[p]*pt_p[p] # PIC pressure
            # get p_I^{t + \Delta t}
            pt_I[base + offset] -= dt * K * volumet_p[p] * Psi_I[i,j] * divvt_p[p]


        Cons[p] -= xt_p[p][0] * xt_p[p][1] # Consistency
        Cons_dx[p] -= float(1.0) * xt_p[p][1] # Gradient consistency
        Cons_dy[p] -= float(1.0) * xt_p[p][0] # Gradient consistency

    for i, j in mt_I:
        if volumet_I[i, j] != 0:
            pt_I[i,j] /= volumet_I[i,j] # pressure-volume parameter to pressure
        if mt_I[i, j][0,0] != 0 and mt_I[i, j][1,1] != 0:# and volumet_I[i, j] != 0:
            vt_I[i,j] = mt_I[i, j].inverse() @ vt_I[i,j] # momentum to velocity
            # ------------------------施加边界条件------------------------
            # i < nodeNum，若 nodeNum=3，即 i = 0，1，2
            # i > num_g - nodeNum - 1，若 num_g=20，即 i = 17，18，19
            # 所以说，意思就是—— x 和 y 方向都预留 3 个 grid nodes 的厚度。
            # -----------------------------------------------------------
            if switch_penaltyEBC:
                pass
            else:
                if i < nodeNum and vt_I[i, j][0] < 0: 
                    vt_I[i, j][0] = 0
                if i > num_g - nodeNum - 1 and vt_I[i, j][0] > 0: 
                    vt_I[i, j][0] = 0
                if j < nodeNum and vt_I[i, j][1] < 0: 
                    vt_I[i, j][1] = 0
                if j > num_g - nodeNum - 1 and vt_I[i, j][1] > 0: 
                    vt_I[i, j][1] = 0
            # if i < nodeNum-1 and vt_I[i, j][0] < 0: vt_I[i, j][0] = 0
            # if i > num_g - nodeNum and vt_I[i, j][0] > 0: vt_I[i, j][0] = 0
            # if j < nodeNum-1 and vt_I[i, j][1] < 0: vt_I[i, j][1] = 0
            # if j > num_g - nodeNum and vt_I[i, j][1] > 0: vt_I[i, j][1] = 0

            # if i == nodeNum-1 and vt_I[i, j][0] < 0: vt_I[i, j][0] = 0
            # if i == num_g - nodeNum and vt_I[i, j][0] > 0: vt_I[i, j][0] = 0
            # if j == nodeNum-1 and vt_I[i, j][1] < 0: vt_I[i, j][1] = 0
            # if j == num_g - nodeNum and vt_I[i, j][1] > 0: vt_I[i, j][1] = 0
  
    for p in xt_p: 
        base = (xt_p[p] * inv_dx - shift).cast(int) #每个 particle 所属的 3x3 support 的左下角点位置
        fx = ( xt_p[p] * inv_dx - base.cast(ti.f64) ) * dx # 向量，由 base 指向 particle

        vt_p_APIC = ti.Vector.zero(ti.f64, 2) # Initialize an APIC velocity vector
        vt_p_FLIP = ti.Vector.zero(ti.f64, 2) # Initialize a FLIP velocity vector
        new_L = ti.Matrix.zero(ti.f64, 2, 2) # Initialize a velocity gradient matrix
        new_divvt_p = ti.cast(0, ti.f64)
        new_p = ti.cast(0, ti.f64)
        

        Psi_I, Psi_Icommax, Psi_Icommay = getRK(xt_p[p], base, a)

        for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)): 
            B_I = ti.Vector([Psi_Icommax[i,j], Psi_Icommay[i,j]]) # 2 by 1 vector, assemble a phi gradient vector
            offset = ti.Vector([i, j])
            # dpos = ( offset.cast(ti.f64) ) * dx - fx # define the distance from the particle to the current node
            new_L += vt_I[base + offset].outer_product(B_I) # define the velocity gradient
            new_divvt_p += vt_I[base + offset].dot(B_I)

            vt_p_APIC += Psi_I[i,j] * vt_I[base + offset] # APIC calculation of velocity
            vt_p_FLIP += Psi_I[i,j] * ( vt_I[base + offset]  - ( mt_I[base + offset].inverse() @ vt_IInitial[base + offset]) ) # FLIP calculation of velocity
            # xt_p[p] += dt * Psi_I[i,j] * g_v
            new_p += Psi_I[i,j] * pt_I[base + offset]

        vt_p_FLIP += vt_p[p]
        vt_p[p] = eta * vt_p_FLIP + (1 - eta) * vt_p_APIC # Define the particle velocity (FLIP blend)
        xt_p[p] += dt * vt_p[p] # Advect particles NFLIP

        Lt_p[p] = new_L
        Ft_p[p] = (ti.Matrix.identity(ti.f64, 2) + dt * Lt_p[p]) @ Ft_p[p] # Deformation gradient update
        U_F, sig_F, V_F = ti.svd(Ft_p[p]) # Singular value decomposition
        J = ti.cast(1.0, ti.f64)
        for d in ti.static(range(dim)):
            J *= sig_F[d, d]
        detF[p] = J
        volumet_p[p] = volume0_p * detF[p] # Update particle volumes using F
        rho_p[p] = rho / detF[p]
        mt_p[p] = volumet_p[p] * rho_p[p]

        divvt_p[p] = new_divvt_p
        pt_p[p] -= dt*K*divvt_p[p]
        pt_p[p] = mixRatio * new_p + (1-mixRatio) * pt_p[p]

        dotepsilon = 0.5 * (Lt_p[p] + Lt_p[p].transpose())
        sigma[p] = - pt_p[p] * ti.Matrix.identity(ti.f64, 2) + 2 * visc * dotepsilon

    if switch_overlinedivv:
        for p in xt_p:
            cellBase = (xt_p[p] * inv_dx).cast(int) # 每个 cellgrid 中心点的位置
            base = (xt_p[p] * inv_dx - shift).cast(int)
            Psi_I, Psi_Icommax, Psi_Icommay = getRK(xt_p[p], base, a)
            for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)):
                offset = ti.Vector([i, j])
                divvt_0_denominator[base + offset] += volume0_p * Psi_I[i,j]
                divvt_0_numerator[base + offset] += divvt_p[p] * volume0_p * Psi_I[i,j]
        for i, j in divvt_0:
            divvt_0[i,j] = divvt_0_numerator[i,j] / (divvt_0_denominator[i,j] + epsilon)
        for p in xt_p:
            # cellBase = (xt_p[p] * inv_dx).cast(int)
            base = (xt_p[p] * inv_dx - shift).cast(int)
            Psi_I, Psi_Icommax, Psi_Icommay = getRK(xt_p[p], base, a)
            for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)):
                offset = ti.Vector([i, j])
                divvt_p[p] += Psi_I[i,j] * divvt_0[base + offset]

    # for p in xt_p: 
        # cellBase = (xt_p[p] * inv_dx).cast(int) 
        # L[p] += 1/3 * (divvt_0[cellBase] - divvt_p[p]) * ti.Matrix.identity(ti.f64, 2)

        # dotepsilon = ti.Matrix.zero(ti.f64, 2, 2)
        # dotepsilon = 0.5 * (Lt_p[p] + Lt_p[p].transpose())
        # sigma[p] = - pt_p[p] * ti.Matrix.identity(ti.f64, 2) + 2 * visc * dotepsilon

x_pos1 = ti.field(dtype=ti.f64, shape=(np_x, np_y))
y_pos1 = ti.field(dtype=ti.f64, shape=(np_x, np_y))
@ti.kernel
def initialize_Cubes():
    for a, b in x_pos1:
        x_pos1[a,b] = (a / (np_x - 1)) * W_fluid + 2 * dx
        y_pos1[a,b] = (b / (np_y - 1)) * H_fluid + 2 * dx
    for i in range(num_p):
        col = i - np_x * ( i // np_x )
        row = i // np_x
        xt_p[i] = [ x_pos1[col,row], y_pos1[col,row] ]
        # x[i] = [ ti.random() * Liquid_Width + 2 * dx, ti.random() * Liquid_Height + 2 * dx]       # Random distribution
        vt_p[i] = [0,0]
        mt_p[i] = volume0_p * rho
        material[i] = 0
        volumet_p[i] = volume0_p
        Ft_p[i] = ti.Matrix([[1.0, 0.0], [0.0, 1.0]])
    
    for i in x_L_left:
        x_L_left[i] = [2 * dx, (2.5 + i) * dx]
        x_L_right[i] = [len_domain + 2 * dx, (2.5 + i) * dx]
        x_L_bot[i] = [(2.5 + i) * dx, 2 * dx]
        x_L_top[i] = [(2.5 + i) * dx, len_domain + 2 * dx]
# -----------------------------------------------------
initialize_Cubes()

# gui = ti.GUI("Taichi MLS-MPM-99", res=512, background_color=0x112F41)
gui = ti.GUI('Window Title', res=512, show_gui=False)

os.getcwd()
if not os.path.exists(filepath):
    os.makedirs(filepath)
if not os.path.exists(vtkpath):
    os.makedirs(vtkpath)

def progressBar(progress, total, color = colorama.Fore.YELLOW):
    percentage = 100 * (progress/float(total))
    bar = '█' *int(percentage) + '-'*(100 - int(percentage))
    print(color+ f"\r|{bar}| {percentage:.2f}%" + " | Current Time: " + str(progress)) #, end = "\r")
    sys.stdout.write("\033[F") #back to previous line 
    sys.stdout.write("\033[K") #clear line 
    if progress >=  total:
        print(colorama.Fore.GREEN + f"\r|{bar}| {percentage:.2f}%" + " | It's done ! | Current time: "+ str(progress), end = "\r")
        print(colorama.Fore.RED)

count = 0
# while not gui.get_event(ti.GUI.ESCAPE, ti.GUI.EXIT):
while (timeTotal < simTime) :
    for s in range(int(5e-4 // dt)): # the 5e-4 here acts as the frame rate
        substep()
        count = count + 1    
        timeTotal = timeTotal + dt
    # print("Current Time: ",timeTotal)
    progressBar(timeTotal, simTime)
    xCoordinate = np.zeros(num_p)
    yCoordinate = np.zeros(num_p)
    zCoordinate = np.zeros(num_p)
    v_x = np.zeros(num_p)
    v_y = np.zeros(num_p)
    v_z = np.zeros(num_p)
    materialID = np.zeros(num_p)
    deformation_step = np.zeros(num_p)
    PoU = np.zeros(num_p)
    Consistency = np.zeros(num_p)
    Consistency_Gradx = np.zeros(num_p)
    Consistency_Grady = np.zeros(num_p)
    vp_mag_step = np.zeros(num_p)
    pressure = np.zeros(num_p)

    sigma_11 = np.zeros(num_p)
    sigma_22 = np.zeros(num_p)
    sigma_12 = np.zeros(num_p)

    sigma_11_dx = np.zeros(num_p)
    sigma_22_dx = np.zeros(num_p)
    sigma_12_dx = np.zeros(num_p)

    xCoordinate[:] = xt_p.to_numpy()[:,0]
    yCoordinate[:] = xt_p.to_numpy()[:,1]
    zCoordinate[:] = 0*xt_p.to_numpy()[:,1]
    deformation_step[:] = detF.to_numpy()[:]
    v_x[:] = vt_p.to_numpy()[:,0]
    v_y[:] = vt_p.to_numpy()[:,1]
    v_z[:] = 0*vt_p.to_numpy()[:,1]
    pressure[:] = - ( sigma.to_numpy()[:,0,0] + sigma.to_numpy()[:,1,1] ) / 3

    sigma_11[:] = sigma.to_numpy()[:,0,0]
    sigma_22[:] = sigma.to_numpy()[:,1,1]
    sigma_12[:] = sigma.to_numpy()[:,0,1]

    PoU[:] = PartitionOfUnity.to_numpy()[:] - float(1.0)
    Consistency[:] = Cons.to_numpy()[:]
    Consistency_Gradx[:] = Cons_dx.to_numpy()[:]
    Consistency_Grady[:] = Cons_dy.to_numpy()[:]

    pointsToVTK('./' + vtkpath + '/points'f'{gui.frame:06d}', xCoordinate, yCoordinate, zCoordinate, 
                data = {"ID" : material.to_numpy(),"simgaxx" : sigma_11, "simgayy" : sigma_22, "simgaxy" : sigma_12, 
                "v_x" : v_x, "v_y" : v_y, "v_z" : v_z, "Pressure" : pressure, "Partition of Unity" : PoU, "Consistency" : Consistency, 
                "Gradx Consistency" : Consistency_Gradx, "Grady Consistency" : Consistency_Grady, "Deformation" : deformation_step,
                "Velocity Mag" : vp_mag_step}, 
                fieldData = {"Velocity": np.concatenate((v_x,v_y,v_z),axis = 0)})
    colors = np.array([0x068587, 0xED553B, 0xEEEEF0], dtype=np.uint32)
    gui.circles(xt_p.to_numpy(), radius=0.8, color=colors[material.to_numpy()])
    gui.show(filepath + '/'f'{gui.frame:06d}.png')

time1 = time.time()
print('Run Time:', time1 - time0)
