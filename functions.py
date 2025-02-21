import taichi as ti
import sys
import colorama
import math


@ti.func
def reproducingKernelFunction(xp, base, a, dx, nodeNum, dim, switch_getRK_Bspline):
    # Initialize the matrix of phi values for each surrounding grid node at the current particle location
    phiMat = ti.Matrix.zero(ti.f64, 3, 3)
    # Initialize the matrix of dphi/dx values for each surrounding grid node at the current particle location
    dphiXMat = ti.Matrix.zero(ti.f64, 3, 3)
    # Initialize the matrix of dphi/dy values for each surrounding grid node at the current particle location
    dphiYMat = ti.Matrix.zero(ti.f64, 3, 3)
    M = ti.Matrix.zero(ti.f64, 3, 3)                       # Initialize the moment matrix
    # Initialize the kernel function vector for weights at each grid node for each coordinate direction (1D)
    w = ti.Matrix.zero(ti.f64, nodeNum, dim)
    weight = ti.Matrix.zero(ti.f64, nodeNum, nodeNum)    # Kernel weights in 2D
    for i, d in ti.static(ti.ndrange(nodeNum, dim)):
        gridNode = float(i + base[d]) * dx            # Current grid node location
        if gridNode >= 0:
            z = abs(xp[d] - float(gridNode)) / a       # Normalized distance from particle to grid node
            if switch_getRK_Bspline:
                if 0 <= z and z < 0.5:                      # Cubic b-spline kernel functions
                    w[i, d] = 2/3 - 4*z**2 + 4*z**3
                elif 1/2 <= z and z < 1:
                    w[i, d] = 4/3 - 4*z + 4*z**2 - (4/3)*z**3
            else:
                if 0 <= z and z < 1:
                    w[i, d] = z - 1
                elif 1 <= z:
                    w[i, d] = 0
    for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)):
        gridNode = [float(i+base[0]) * dx, float(j+base[1]) * dx]  # Current grid node location
        if gridNode[0] >= 0 and gridNode[1] >= 0:
            weight[i, j] = w[i, 0] * w[j, 1]  # Define kernel function weights in 2D
            Pxi = (ti.Vector([1.0, xp[0] - gridNode[0], xp[1] - gridNode[1]]))  # Define P(xi - xp)
            if weight[i, j] != 0:
                M += weight[i, j] * Pxi @ Pxi.transpose()  # Define the moment matrix
    M_inv = M.inverse()
    for i, j in ti.static(ti.ndrange(nodeNum, nodeNum)):  # Loop over neighboring grid nodes
        gridNode = [float(i+base[0]) * dx, float(j+base[1]) * dx]  # Current grid node location
        if weight[i, j] != 0:
            if gridNode[0] >= 0 and gridNode[1] >= 0:
                Pxi = ti.Vector([1.0, xp[0] - gridNode[0], xp[1] - gridNode[1]])  # Define P(xi - xp)
                Pxp = ti.Vector([1.0, xp[0] - xp[0], xp[1] - xp[1]])
                dPxpX = ti.Vector([0.0, -1.0, 0.0])
                dPxpY = ti.Vector([0.0, 0.0, -1.0])

                phi = weight[i, j] * (Pxp.transpose() @ M_inv @ Pxi)                   # Define phi
                dphi_x1 = weight[i, j] * (dPxpX.transpose() @ M_inv @ Pxi)             # Define dphi/dx
                dphi_x2 = weight[i, j] * (dPxpY.transpose() @ M_inv @ Pxi)             # Define dphi/dy

                phiMat[i, j] = phi[0]
                dphiXMat[i, j] = dphi_x1[0]
                dphiYMat[i, j] = dphi_x2[0]

    return phiMat, dphiXMat, dphiYMat

# Function to generate file paths based on simulation conditions


def format_exp(x, n, d=6):
    significand = x / 10 ** n
    exp_sign = '+' if n >= 0 else ''
    # exp_sign = '+' if n >= 0 else '-'
    return f'{significand:.{d}f}e{exp_sign}{n:02d}'


def createFilePaths(config):
    filepath = "mov"
    vtkpath = "vtk"

    if config.numericalSettings.pressureMixingRatio == 1:
        filepath = filepath + "_mixed"
        vtkpath = vtkpath + "_mixed"
    elif config.numericalSettings.pressureMixingRatio == 0:
        filepath = filepath + "_pointwise"
        vtkpath = vtkpath + "_pointwise"

    filepath = filepath + "_dt" + format_exp(config.numericalSettings.timeStep,
                                             math.floor(math.log10(config.numericalSettings.timeStep)), 0)
    vtkpath = vtkpath + "_dt" + format_exp(config.numericalSettings.timeStep, math.floor(math.log10(config.numericalSettings.timeStep)), 0)

    if config.numericalSettings.switchPenaltyEBC:
        filepath = filepath + "_betaNor" + format_exp(config.numericalSettings.penalty,
                                                      math.floor(math.log10(config.numericalSettings.penalty)), 0)
        vtkpath = vtkpath + "_betaNor" + format_exp(config.numericalSettings.penalty,
                                                    math.floor(math.log10(config.numericalSettings.penalty)), 0)

    if config.numericalSettings.switchOverlinedivv:
        filepath = filepath + "_divvBar"
        vtkpath = vtkpath + "_divvBar"

    return filepath, vtkpath


def progressBar(progress, total, color=colorama.Fore.YELLOW):
    percentage = 100 * (progress/float(total))
    bar = '█' * int(percentage) + '-'*(100 - int(percentage))
    print(color + f"\r|{bar}| {percentage:.2f}%" + " | Current Time: " + str(progress))  # , end = "\r")
    sys.stdout.write("\033[F")  # back to previous line
    sys.stdout.write("\033[K")  # clear line
    if progress >= total:
        print(colorama.Fore.GREEN + f"\r|{bar}| {percentage:.2f}%" + " | It's done ! | Current time: " + str(progress), end="\r")
        print(colorama.Fore.RED)
