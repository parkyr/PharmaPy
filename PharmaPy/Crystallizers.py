# -*- coding: utf-8 -*-
"""
Created on Tue Jan 14 19:31:56 2020
@author: dcasasor
"""

# import autograd.numpy as np


from assimulo.solvers import CVode
from assimulo.problem import Explicit_Problem

from PharmaPy.Phases import classify_phases
from PharmaPy.Streams import LiquidStream, SolidStream
from PharmaPy.Phases import LiquidPhase, SolidPhase
from PharmaPy.MixedPhases import Slurry, SlurryStream
from PharmaPy.Commons import reorder_sens, plot_sens, trapezoidal_rule
from PharmaPy.NameAnalysis import get_dict_states
from PharmaPy.jac_module import numerical_jac, numerical_jac_central, dx_jac_x

import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from matplotlib import cm
from matplotlib.ticker import AutoMinorLocator
from matplotlib.colors import LightSource

from scipy.optimize import newton

import copy
import string

try:
    from jax import jacfwd
    import jax.numpy as jnp
    # from autograd import jacobian as autojac
    # from autograd import make_jvp
except:
    print(
        'JAX is not available to perform automatic differentiation.'
        'Install JAX if supported by your operating system.'
        'JAX is available for UNIX-type operating systems (Linux, Mac).')

import numpy as np

eps = np.finfo(float).eps
gas_ct = 8.314  # J/mol/K


class _BaseCryst:
    np = np
    # @decor_states

    def __init__(self, mask_params,
                 method, target_ind, scale,
                 isothermal, temp_control, adiabatic,
                 rad_zero,
                 reset_states, name_species,
                 u_ht, vol_ht, ht_media, basis, jac_type):
        """ Construct a Crystallizer object
        Parameters
        ----------
        oper_mode : str
            Operation mode of the reactor. It takes one of the following
            values: 'Batch', 'MSMPR', 'Semibatch'. If 'Semibatch', it is
            assumed that an antisolvent stream is entering the tank.
        """

        if jac_type == 'AD':
            try:
                import jax.numpy as np
                _BaseCryst.np = np
            except:
                pass

        self.distributed_uo = False
        self.mask_params = mask_params
        self.basis = basis
        self.adiabatic = adiabatic

        # ---------- Building objects
        self._Phases = None
        self._KinInstance = None
        self.jac_type = jac_type

        self.target_ind = target_ind

        self.scale = scale
        self.scale_flag = True

        self.isothermal = isothermal
        if temp_control is not None:
            self.isothermal = False

        self.ht_media = ht_media

        # Control
        self.temp_control = temp_control
        self.params_control = None

        self.method = method
        self.rad = rad_zero

        self.dx = None
        self.sensit = None

        # ---------- Create jacobians (autodiff)
        self.jac_states_vals = None
        # if method == 'moments':
        #     self.jac_states_fun = autojac(self.unit_model, 1)
        #     self.jac_params_fun = autojac(self.unit_model, 2)
        # elif method == 'fvm':
        #     self.jac_states_fun = make_jvp(self.fvm_method)

        #     # self.jac_params_fun = autojac(self.fvm_method, 1)
        #     self.jac_params_fun = None

        # Outlets
        self.reset_states = reset_states
        self.elapsed_time = 0
        self.tempProf = []
        self.wConcProf = []
        self.distribProf = []
        self.timeProf = []
        self.tempProfHt = []

        self.__original_prof__ = {
            'tempProf': [], 'concProf': [], 'distribProf': [], 'timeProf': [],
            'elapsed_time': 0, 'scale_flag': True
        }

        # ---------- Names
        self.name_species = name_species

        self.states_uo = ['mass_conc']
        self.names_states_in = ['mass_conc']

        if not self.isothermal and self.temp_control is None:
            self.states_uo.append('temp')

        self.names_upstream = None
        self.bipartite = None

        # Other parameters
        self.u_ht = u_ht

        # Slurry phase
        self.Slurry = None

    @property
    def Phases(self):
        return self._Phases

    @Phases.setter
    def Phases(self, phases):
        if isinstance(phases, (list, tuple)):
            self._Phases = phases
        elif isinstance(phases, Slurry):
            self._Phases = phases.Phases
        elif phases.__module__ == 'PharmaPy.Phases':
            if self._Phases is None:
                self._Phases = [phases]
            else:
                self._Phases.append(phases)
        else:
            raise RuntimeError('Please provide a list or tuple of phases '
                               'objects')

        if isinstance(phases, Slurry):
            self.Slurry = phases
        elif isinstance(self._Phases, (list, tuple)):
            if len(self._Phases) > 1:

                # Mixed phase
                self.Slurry = Slurry()
                self.Slurry.Phases = self._Phases

                # self.init_mass = self.Liquid_1.mass + self.Solid_1.mass
                # self.init_liq = copy.copy(self.Liquid_1.mass)

                self.__original_phase__ = copy.deepcopy(
                    self.Slurry.Liquid_1.__dict__)

        if self.Slurry is not None:
            self.vol_slurry = copy.copy(self.Slurry.vol_slurry)
            classify_phases(self)  # Solid_1, Liquid_1...

            self.kron_jtg = np.zeros_like(self.Liquid_1.mass_frac)
            self.kron_jtg[self.target_ind] = 1

            # ---------- Names
            # Moments
            if self.method == 'moments':
                name_mom = [r'\mu_{}'.format(ind) for ind
                            in range(self.Solid_1.num_mom)]
                name_mom.append('C')

            # Species
            if self.name_species is None:
                num_sp = len(self.Liquid_1.mass_frac)
                self.name_species = list(string.ascii_uppercase[:num_sp])

    @property
    def KinInstance(self):
        return self._KinInstance

    @KinInstance.setter
    def KinInstance(self, instance):
        self._KinInstance = instance

        name_params = self._KinInstance.name_params
        if self.mask_params is None:
            self.mask_params = [True] * self._KinInstance.num_params
            self.name_params = name_params

        else:
            self.name_params = [name for ind, name in enumerate(name_params)
                                if self.mask_params[ind]]

        self.mask_params = np.array(self.mask_params)

        ind_true = np.where(self.mask_params)[0]
        ind_false = np.where(~self.mask_params)[0]

        self.params_fixed = self.KinInstance.merge_params()[ind_false]

        self.ind_maskpar = np.argsort(np.concatenate((ind_true, ind_false)))

    def reset(self):
        copy_dict = copy.deepcopy(self.__original_prof__)
        self.__dict__.update(copy_dict)

        for phase, di in zip(self.Phases, self.__original_phases__):
            phase.__dict__.update(di)

    def method_of_moments(self, mu, conc, temp, params, rho_cry, vol=1):
        kv = self.Solid_1.kv

        # Kinetics
        if self.basis == 'mass_frac':
            rho_liq = self.Liquid_1.getDensity()
            comp_kin = conc / rho_liq
        else:
            comp_kin = conc

        # Kinetic terms
        nucl, growth, dissol = self.KinInstance.get_kinetics(
            comp_kin[self.target_ind], temp, kv, mu[3]*(1e-6)**3)

        growth = growth * self.KinInstance.alpha_fn(conc)

        ind_mom = np.arange(1, len(mu))

        # Model
        dmu_zero_dt = np.atleast_1d(nucl * vol)
        dmu_1on_dt = ind_mom * (growth + dissol) * mu[:-1] + \
            nucl * self.rad**ind_mom

        dmu_dt = np.concatenate((dmu_zero_dt, dmu_1on_dt))

        # material balance in g_sol / mL_sln (# G in um and u_2 in um**2)
        mass_transf = np.atleast_1d(rho_cry * kv * (
            3*(growth + dissol)*mu[2] + nucl*self.rad**3)) * (1e-6)**3

        return dmu_dt, mass_transf

    def fvm_method(self, csd, moms, conc, temp, params, rho_cry,
                   output='dstates', vol=1):
        mu_2, mu_3 = moms

        kv_cry = self.Solid_1.kv

        # Kinetic terms
        # self.KinInstance.params = params
        if self.basis == 'mass_frac':
            rho_liq = self.Liquid_1.getDensity()
            comp_kin = conc / rho_liq
        else:
            comp_kin = conc

        nucl, growth, dissol = self.KinInstance.get_kinetics(
            comp_kin[self.target_ind], temp, kv_cry, mu_3)

        nucl = nucl * self.scale * vol
        impurity_factor = self.KinInstance.alpha_fn(conc)
        growth = growth * impurity_factor  # um/s
        dissol = dissol  # um/s

        f_ghost = np.concatenate(([csd[0]], csd, [csd[-1]]))

        # Flux source terms
        f_diff = np.diff(f_ghost)

        if growth > 0:
            theta = (f_diff[:-1] + eps) / (f_diff[1:] + eps)
        else:
            theta = (f_diff[1:] + eps) / (f_diff[:-1] + eps)

        # Van-Leer limiter
        limiter = (np.abs(theta) + theta) / (1 + np.abs(theta))

        growth_term = growth * (csd[:-1] + 0.5 * limiter[:-1] * f_diff[1:-1])
        dissol_term = dissol * (csd[1:] - 0.5 * limiter[1:] * f_diff[1:-1])

        flux_internal = growth_term + dissol_term

        flux = np.concatenate(([nucl], flux_internal, [0]))

        if output == 'flux':
            return flux  # TODO: isn't it necessary to divide by dx?
        elif 'dstates':
            dcsd_dt = -np.diff(flux) / self.dx

            # balance in kg_sol / m3_sln
            mass_transfer = rho_cry * kv_cry * (
                3*(growth + dissol)*mu_2 + nucl*self.rad**3) * (1e-6)**3

            # if growth > 0:
            #     print('Impurity factor: %.2f, growth: %.4e, dissol: %.4e'
            #           % (impurity_factor, growth, dissol))

            return dcsd_dt, np.array(mass_transfer)

    def unit_model(self, time, states, params):

        # ---------- Prepare inputs
        if len(self.params_fixed) > 1:
            params_all = np.concatenate((params, self.params_fixed))
            params_all = params_all[self.ind_maskpar]
        else:
            params_all = params

        self.KinInstance.set_params(params_all)

        num_material = self.num_distr + self.num_species

        distr = states[:self.num_distr]
        w_conc = states[self.num_distr:num_material]

        ind_bces = num_material

        # Inputs
        u_input = self.get_inputs(time)

        # Check for volume
        if 'vol' in self.states_uo:
            vol = states[ind_bces]
            ind_bces += 1
        else:
            vol = self.vol_slurry

        # Check for temperature
        if 'temp_ht' in self.states_uo:
            temp, temp_ht = states[[ind_bces, ind_bces + 1]]
        elif 'temp' in self.states_uo:
            temp = states[ind_bces]
            temp_ht = None
        elif self.temp_control is not None:
            temp = self.temp_control(time, self.temp,
                                     *self.params_control['temp'],
                                     t_zero=self.elapsed_time)
        else:
            temp = self.Liquid_1.temp

        # ---------- Physical properties
        self.Liquid_1.updatePhase(mass_conc=w_conc)
        self.Liquid_1.temp = temp
        self.Solid_1.temp = temp

        rhos_susp = self.Slurry.getDensity(temp=temp)

        name_unit = self.__class__.__name__

        if name_unit == 'BatchCryst':
            rhos = rhos_susp
            h_in = None
            phis_in = None
        elif name_unit == 'SemibatchCryst' or name_unit == 'MSMPR':
            # massfrac_in = self.Liquid_1.mass_conc_to_frac(w_conc, basis='mass')
            inlet_temp = u_input['temp']

            if self.Inlet.__module__ == 'PharmaPy.MixedPhases':
                # self.Inlet.Liquid_1.updatePhase(mass_frac=massfrac_in)
                rhos_in = self.Inlet.getDensity(temp)

                inlet_distr = u_input['num_distrib']

                mom_in = self.Inlet.Solid_1.getMoments(inlet_distr, mom_num=3)

                phi_in = 1 - self.Inlet.Solid_1.kv * mom_in
                phis_in = np.concatenate([phi_in, 1 - phi_in])

                h_in = self.Inlet.getEnthalpy(inlet_temp, phis_in,
                                              rhos_in)
            else:
                # self.Inlet.updatePhase(mass_frac=massfrac_in)
                rho_liq_in = self.Inlet.getDensity(temp=inlet_temp)
                rho_sol_in = None

                rhos_in = np.array([rho_liq_in, rho_sol_in])
                h_in = self.Inlet.getEnthalpy(temp=inlet_temp)

                phis_in = [1, 0]

            rhos = [rhos_susp, rhos_in]

        if self.method == 'moments':
            moms = distr[[2, 3]] * (1e-6)**np.array([2, 3])
        else:
            moms = self.Solid_1.getMoments(distr / self.scale, mom_num=[2, 3])

        # Balances
        material_bces, cryst_rate = self.material_balances(time, distr, w_conc,
                                                           temp, vol,
                                                           params, u_input,
                                                           rhos, moms, phis_in)

        if 'temp' in self.states_uo:
            energy_bce = self.energy_balances(time, distr, w_conc,
                                              temp, temp_ht, vol,
                                              params, cryst_rate,
                                              u_input,
                                              rhos, moms, h_in)

            balances = np.append(material_bces, energy_bce)
        else:
            balances = material_bces

        return balances

    def unit_jacobians(self, time, states, sens, params, fy, v_vector):
        if sens is not None:
            jac_states = self.jac_states_fun(time, states, params)
            jac_params = self.jac_params_fun(time, states, params)

            dsens_dt = np.dot(jac_states, sens) + jac_params

            if not isinstance(dsens_dt, np.ndarray):
                dsens_dt = dsens_dt._value

            return dsens_dt
        elif v_vector is not None:
            _, jac_v = self.jac_states_fun(time, states, params)(v_vector)

            return jac_v
        else:
            jac_states = self.jac_states_fun(time, states, params)

            if not isinstance(jac_states, np.ndarray):
                jac_states = jac_states._value

            return jac_states

    def jac_states_numerical(self, time, states, params, return_only=True):
        if return_only:
            return self.jac_states_vals
        else:
            def wrap_states(st): return self.unit_model(time, st, params)

            abstol = self.sundials_opt['atol']
            reltol = self.sundials_opt['rtol']
            jac_states = numerical_jac_central(wrap_states, states,
                                               dx=dx_jac_x,
                                               abs_tol=abstol, rel_tol=reltol)

            return jac_states

    def jac_params_numerical(self, time, states, params):
        def wrap_params(theta): return self.unit_model(time, states, theta)

        abstol = self.sundials_opt['atol']
        reltol = self.sundials_opt['rtol']
        p_bar = self.sundials_opt['pbar']

        dp = np.abs(p_bar) * np.sqrt(max(reltol, eps))

        jac_params = numerical_jac_central(wrap_params, params,
                                           dx=dp,
                                           abs_tol=abstol, rel_tol=reltol)

        return jac_params

    def jac_states_ad(self, time, states, params):
        def wrap_states(st): return self.unit_model(time, st, params)
        jac_states = jacfwd(wrap_states)(states)

        return jac_states

    def jac_params_ad(self, time, states, params):
        def wrap_params(theta): return self.unit_model(time, states, theta)
        jac_params = jacfwd(wrap_params)(params)

        return jac_params

    def rhs_sensitivity(self, time, states, sens, params):
        jac_params_vals = self.jac_params_fn(time, states, params)

        jac_states_vals = self.jac_states_fn(time, states, params,
                                             return_only=False)

        rhs_sens = np.dot(jac_states_vals, sens) + jac_params_vals

        self.jac_states_vals = jac_states_vals

        return rhs_sens

    def set_ode_problem(self, eval_sens, states_init, params_mergd,
                        jacv_prod):
        if eval_sens:
            problem = Explicit_Problem(self.unit_model, states_init,
                                       t0=self.elapsed_time,
                                       p0=params_mergd)

            if self.jac_type == 'finite_diff':
                self.jac_states_fn = self.jac_states_numerical
                self.jac_params_fn = self.jac_params_numerical

                problem.jac = self.jac_states_fn
                problem.rhs_sens = self.rhs_sensitivity

            elif self.jac_type == 'AD':
                self.jac_states_fn = self.jac_states_ad
                self.jac_params_fn = self.jac_params_ad

                problem.jac = self.jac_states_fn
                problem.rhs_sens = self.rhs_sensitivity
            elif self.jac_type == 'analytical':
                self.jac_states_fn = self.jac_states
                self.jac_params_fn = self.jac_params

                problem.jac = self.jac_states_fn
                problem.rhs_sens = self.rhs_sensitivity

            elif self.jac_type is None:
                pass
            else:
                raise NameError("Bad string value for the 'jac_type' argument")

        else:
            def unit_model(time, states): return self.unit_model(
                time, states, params_mergd)

            problem = Explicit_Problem(unit_model, states_init,
                                       t0=self.elapsed_time)
            # ----- Jacobian callables
            if self.method == 'moments':
                # w.r.t. states
                # problem.jac = lambda time, states: \
                #     self.unit_jacobians(time, states, None, params_mergd,
                #                         None, None)

                pass

            elif self.method == 'fvm':
                # J*v product (AD, slower than the one used by SUNDIALS)
                if jacv_prod:
                    problem.jacv = lambda time, states, fy, v: \
                        self.unit_jacobians(time, states, None, params_mergd,
                                            fy, v)

        return problem

    def solve_unit(self, runtime=None, time_grid=None, params_control=None,
                   eval_sens=False,
                   jac_v_prod=False, verbose=True, test=False,
                   sundials_opts=None):

        self.params_control = params_control

        # ---------- Solid phase states
        if 'vol' in self.states_uo:
            if self.method == 'moments':
                init_solid = self.Solid_1.moments
                exp = np.arange(0, self.Solid_1.num_mom)
                init_solid *= self.scale**exp

            # eps_init = init_solid[3] * self.Solid_1.kv

            elif self.method == '1D-FVM':
                x_grid = self.Solid_1.x_distrib
                init_solid = self.Solid_1.distrib * self.scale

        else:
            if self.method == 'moments':
                init_solid = self.Slurry.moments
                exp = np.arange(0, self.Slurry.num_mom)
                init_solid *= self.scale**exp

            # eps_init = init_solid[3] * self.Solid_1.kv

            elif self.method == '1D-FVM':
                x_grid = self.Slurry.x_distrib
                init_solid = self.Slurry.distrib * self.scale

        self.num_distr = len(init_solid)

        self.dx = self.Slurry.dx
        self.x_grid = self.Slurry.x_distrib

        # ---------- Liquid phase states
        init_liquid = self.Liquid_1.mass_conc.copy()
        self.mask_composit = np.arange(len(init_liquid)) == self.target_ind

        # Create indexes for reordering states
        ind_max = np.argmax(init_liquid)

        ind_imp = np.arange(len(init_liquid))
        ind_imp = np.where(
            (ind_imp != self.target_ind) & (ind_imp != ind_max))[0]

        self.ind_imp = ind_imp

        self.composit_order = np.argsort(
            np.concatenate(([self.target_ind, ind_max], ind_imp))
        )

        self.num_species = len(init_liquid)

        if 'vol' in self.states_uo:  # Batch or semibatch
            vol_init = self.Slurry.getTotalVol()
            init_susp = np.append(init_liquid, vol_init)

            init_solid *= vol_init  # Total particle number
        else:
            init_susp = init_liquid

        self.composit = init_liquid
        self.temp = self.Liquid_1.temp

        if self.reset_states:
            self.reset()

        # ---------- Read time
        if runtime is not None:
            final_time = runtime + self.elapsed_time

        if time_grid is not None:
            final_time = time_grid[-1]

        if self.scale_flag:
            self.scale_flag = False

        states_init = np.append(init_solid, init_susp)

        if 'temp_ht' in self.states_uo:
            states_init = np.concatenate(
                (states_init, [self.Liquid_1.temp, self.Liquid_1.temp]))
        elif 'temp' in self.states_uo:
            states_init = np.append(states_init, self.Liquid_1.temp)

        merged_params = self.KinInstance.merge_params()[self.mask_params]

        # ---------- Create problem
        problem = self.set_ode_problem(eval_sens, states_init,
                                       merged_params, jac_v_prod)

        # ---------- Set solver
        # General
        solver = CVode(problem)
        solver.iter = 'Newton'
        solver.discr = 'BDF'

        if sundials_opts is not None:
            for name, val in sundials_opts.items():
                setattr(solver, name, val)

        self.sundials_opt = solver.get_options()

        if eval_sens:
            solver.sensmethod = 'SIMULTANEOUS'
            solver.suppress_sens = False
            solver.report_continuously = True

        if self.method == '1D-FVM':
            solver.linear_solver = 'SPGMR'  # large, sparse systems

        if not verbose:
            solver.verbosity = 50

        # ---------- Solve model
        time, states = solver.simulate(final_time, ncp_list=time_grid)

        self.retrieve_results(time, states)

        # ---------- Organize sensitivity
        if eval_sens:
            sensit = []
            for elem in solver.p_sol:
                sens = np.array(elem)
                sens[0] = 0  # correct NaN's at t = 0 for sensitivities
                sensit.append(sens)

            self.sensit = sensit

            return time, states, sensit
        else:
            return time, states

    def paramest_wrapper(self, params, t_vals, kwargs_solve={}):

        self.KinInstance.params = params
        self.elapsed_time = 0

        t_prof, states, sens = self.solve_unit(time_grid=t_vals,
                                               **kwargs_solve)

        sens_sep = reorder_sens(sens, separate_sens=True)
        if self.method == 'moments':
            mu = states[:, :self.num_species]
            mu_zero = mu[:, 0][..., np.newaxis]

            sens_zero = sens_sep[0]
            for ind, sens_j in enumerate(sens_sep[1:-1]):
                mu_j = mu[:, ind + 1][..., np.newaxis]
                sens_sep[ind + 1] = (sens_j * mu_zero - sens_zero * mu_j) / \
                    (mu_zero**2 + eps)

            states[:, 1:-1] = (mu[:, 1:].T/mu_zero.flatten()).T  # mu_j/mu_0

        sens_ordered = np.vstack(sens_sep)

        return states, sens_ordered

    def flatten_states(self):
        if type(self.timeProf) is list:
            self.distribProf = np.vstack(self.distribProf)

            self.wConcProf = np.concatenate(self.wConcProf)
            self.tempProf = np.concatenate(self.tempProf)
            self.timeProf = np.concatenate(self.timeProf)
            if len(self.tempProfHt) > 0:
                self.tempProfHt = np.concatenate(self.tempProfHt)

            # Update phases
            self.Liquid_1.tempProf = self.tempProf

            self.Liquid_1.massconcProf = self.wConcProf

            self.Liquid_1.timeProf = self.timeProf

            self.Solid_1.tempProf = self.tempProf
            self.Solid_1.timeProf = self.timeProf

            if self.method == 'moments':
                self.Solid_1.momProf = self.distribProf
            else:
                distrProf = self.distribProf
                self.Solid_1.distribProf = distrProf
                momProf = self.Solid_1.getMoments(distrProf,
                                                  mom_num=[0, 1, 2, 3, 4])

                # for ind in range(momProf.shape[1]):
                #     momProf[:, ind] *= (1e-6)**ind

                self.Solid_1.momProf = momProf

    def plot_profiles(self, fig_size=None, relative_mu0=False,
                      title=None, time_div=1):

        self.flatten_states()

        sat_conc = self.KinInstance.get_solubility(self.tempProf)
        supersat = self.wConcProf[:, self.target_ind] - sat_conc

        if self.KinInstance.rel_super:
            supersat *= 1 / sat_conc

        self.supsatProf = supersat

        # if self.method == 'moments':
        mu = self.Solid_1.momProf
        num_mu = mu.shape[1]
        idx_mom = np.arange(mu.shape[1])

        # Sauter diameter
        if mu.shape[1] > 4:
            mu_4 = mu[:, 4]
        else:
            mu_4 = None

        if relative_mu0:
            # plot if there is at least one particle
            ind_part = np.argmax(mu[:, 0] > 1)
            num_mu -= 1

            div = mu[ind_part:, 0]
            mu_plot = mu[ind_part:, 1:4]
            if self.method == '1D-FVM':
                mu_plot[:, 0] *= 1e6  # express mean diam in um

            time_plot = self.timeProf[ind_part:]

            if mu_4 is not None:
                mu_4 = mu_4[ind_part:]
                num_mu -= 1

        else:
            div = 1
            mu_plot = mu
            time_plot = self.timeProf

        if 'vol' in self.states_uo:
            num_plots = num_mu + 3
        else:
            num_plots = num_mu + 2

        num_cols = bool(num_plots // 2) + 1
        num_rows = num_plots // 2 + num_plots % 2

        fig, axes = plt.subplots(num_rows, num_cols, figsize=fig_size)

        # ---------- Moments
        for ind, col in enumerate(mu_plot.T):
            axes.flatten()[ind].plot(time_plot/time_div,
                                     col/div)

            if relative_mu0:
                denom = '/\mu_{0}$'
                units = ['($\mu m$)'] + ['($m^%i$)' % i for i in idx_mom[2:]]
            else:
                denom = '$'
                if 'vol' in self.states_uo:
                    per = ''
                else:
                    per = ' m^{-3}'
                # exp = int(np.log10(self.scale))
                units = ['$(\mathregular{\# \: %s)}$' % per,
                         ' $\mathregular{(m \: %s)}$' % per] + \
                    [' $\mathregular{(m^%i \: %s)}$' % (i, per)
                     for i in idx_mom[2:]]

            axes.flatten()[ind].set_ylabel(r'$\mu_{}'.format(
                ind + relative_mu0) + denom + units[ind])

        # ---------- Sauter diameter
        if mu_4 is not None:
            if relative_mu0:
                ax_sauter = axes[0, 0].twinx()
                ax_sauter.plot(time_plot/time_div, mu_4 / mu_plot[:, 2] * 1e6,
                               '--')
                ax_sauter.set_ylabel('$\mu_4/\mu_3$ ($\mu m$)')

                ax_sauter.spines['top'].set_visible(False)

                ax_sauter.set_title('Mean diameter')

                # axes[0, 0].legend(('$\mu_1/\mu_0$', '$\mu_4/\mu_3$'))

        # ---------- Temperature
        ax_temp = axes.flatten()[ind + 1]
        ax_temp.plot(self.timeProf/time_div, self.Liquid_1.tempProf)

        if len(self.tempProfHt) > 0:
            ax_temp.plot(self.timeProf/time_div, self.tempProfHt, '--')

        ax_temp.set_ylabel(r'$T$ (K)')

        ax_temp.legend(('tank', 'jacket'), fontsize=7, loc='best')

        # ---------- Concentration
        ax_conc = axes.flatten()[ind + 2]
        ax_conc.plot(self.timeProf/time_div,
                     self.wConcProf[:, self.target_ind], 'k')

        target_id = self.name_species[self.target_ind]

        if self.basis == 'mass_frac':
            ax_conc.set_ylabel('$w_{%s, liq}$ ($kg/kg$)' % target_id)
        else:
            ax_conc.set_ylabel('$C_{%s, liq}$ ($kg/m^3$)' % target_id)

        ax_conc.plot(self.timeProf/time_div, sat_conc, '--k', alpha=0.4)

        # Supersaturation
        ax_supsat = ax_conc.twinx()
        ax_supsat.plot(self.timeProf / time_div, supersat)
        color = ax_supsat.lines[0].get_color()
        # ax_supsat.axhline(0, ls='--', alpha=0.6)

        if self.KinInstance.rel_super:
            ax_supsat.set_ylabel(
                'Supersaturation\n' + r'$\left( \frac{C - C_{sat}}{C_{sat}} \right)$')
        else:
            ax_supsat.set_ylabel('Supersaturation\n($kg/kg_{liq}$)')

        ax_supsat.spines['right'].set_color(color)
        ax_supsat.tick_params(colors=color)
        ax_supsat.yaxis.label.set_color(color)
        ax_supsat.spines['top'].set_visible(False)

        # ---------- Volume
        if 'vol' in self.states_uo:
            ax_vol = axes.flatten()[num_mu + 2]
            ax_vol.plot(self.timeProf/time_div, self.volProf)
            ax_vol.set_ylabel('$V_L$ ($m^3$)')

        # ---------- Final touches
        if len(axes.flatten()) > num_plots:
            fig.delaxes(axes.flatten()[-1])

        for ind, ax in enumerate(axes.flatten()):
            ax.spines['right'].set_visible(False)
            ax.spines['top'].set_visible(False)

            ax.xaxis.set_minor_locator(AutoMinorLocator(2))
            ax.yaxis.set_minor_locator(AutoMinorLocator(2))

        fig.suptitle(title)
        fig.tight_layout()

        if time_div == 1:
            fig.text(0.5, 0, 'time (s)', ha='center')

        return fig, axes, ax_supsat

    def plot_csd(self, view_angles=(20, -20), fig_size=None, time_eval=None,
                 vol_based=False, time_div=1, logx=True):
        self.flatten_states()

        if self.method != '1D-FVM':
            raise RuntimeError('No 3D data to show. Run crystallizer with the '
                               'FVM method')

        if time_eval is None:
            if vol_based:
                csd_plot = self.distribVolProf
            else:
                csd_plot = self.distribProf

            ls = LightSource(azdeg=0, altdeg=65)
            rgb = ls.shade(csd_plot, plt.cm.RdYlBu)

            time_plot = self.timeProf / time_div

            x_mesh, t_mesh = np.meshgrid(np.log10(self.x_grid), time_plot)

            fig = plt.figure(figsize=fig_size)

            ax = fig.gca(projection='3d')
            ax.plot_surface(x_mesh, t_mesh, csd_plot,
                            facecolors=rgb, antialiased=False,
                            linewidth=0,
                            rstride=2, cstride=2)
            # ax.view_init(*view_angles)

            # Edit
            if time_div == 1:
                ax.set_xlabel('time (s)')
            ax.set_ylabel(r'$\log_{10}(L)$ (L in $\mu m$)')
            ax.set_zlabel(r'$f \left( \frac{\#}{m^3 \mu m} \right)$')

            ax.dist = 11

            # ax.invert_yaxis()

        else:
            time_ind = np.argmin(abs(time_eval - self.timeProf))

            if vol_based:
                csd_time = self.distribVolProf[time_ind]
            else:
                csd_time = self.distribProf[time_ind]

            fig, ax = plt.subplots(figsize=fig_size)
            if logx:
                ax.semilogx(self.x_grid, csd_time, '-o', mfc='None')
            else:
                ax.plot(self.x_grid, csd_time, '-o', mfc='None')
            # ax.set_xlabel(r'$x$ ($\mu m$)')
            ax.set_xlabel(r'$x$ ($\mu m$)')

            if 'vol' in self.states_uo:
                distrib_name = '\\tilde{f}'
                div = ''
            else:
                distrib_name = 'f'
                div = 'm^3 \cdot '

            if vol_based:
                ax.set_ylabel(
                    # r'$f$ $\left( \frac{m^3}{m^3 \cdot \mu m} \right)$')
                    r'$%s_v$ $\left( \frac{m^3}{%s \mu m} \right)$' %
                    (distrib_name, div))
                # ax.set_xscale('log')
            else:
                ax.set_ylabel(
                    # r'$f$ $\left( \frac{\#}{m^3 \cdot \mu m} \right)$')
                    r'$%s$ $\left( \frac{\#}{%s \mu m} \right)$' %
                    (distrib_name, div))

            ax.spines['right'].set_visible(False)
            ax.spines['top'].set_visible(False)

            ax.text(1, 1.04, 't = {:.0f} s'.format(time_eval),
                    transform=ax.transAxes, ha='right')

        return fig, ax

    def plot_csd_2d(self, fig_size=(5, 4)):
        self.flatten_states()

        if self.method != '1D-FVM':
            raise RuntimeError('No 3D data to show. Run crystallizer with the '
                               'FVM method')

        x_mesh, t_mesh = np.meshgrid(self.x_grid, self.timeProf)

        fig, ax = plt.subplots(figsize=fig_size)

        cf = ax.contourf(x_mesh.T, t_mesh.T, self.distribProf.T,
                         cmap=cm.coolwarm, levels=50)
        cbar = fig.colorbar(cf)

        if self.scale == 1:
            cbar.ax.set_ylabel(r'$f$ $\left( \frac{\#}{m^3 \mu m} \right)$')
        else:
            exp = int(np.log10(self.scale))
            cbar.ax.set_ylabel(
                r'$f$ $\times 10^{%i}$ $\left( \frac{\#}{m^3 \mu m} \right)$ ' % exp)

        # Edit
        ax.set_xlabel(r'size ($\mu m$)')
        ax.set_ylabel('time (s)')
        ax.invert_yaxis()

        return fig, ax

    def plot_sens(self, mode='per_parameter'):
        if type(self.timeProf) is list:
            self.flatten_states()

        if self.sensit is None:
            raise AttributeError("No sensitivities detected. Run the unit "
                                 " with 'eval_sens'=True")

        if mode == 'per_parameter':
            sens_data = self.sensit
        elif mode == 'per_state':
            sens_data = reorder_sens(self.sensit, separate_sens=True)

        # Name states
        name_mom = ['\mu_%i' % i for i in range(self.num_distr)]
        name_conc = ["C_{" + self.name_species[ind] + "}"
                     for ind in range(len(self.Liquid_1.name_species))]

        name_others = []
        if 'vol' in self.states_uo:
            name_others.append('vol')

        if 'temp' in self.states_uo:
            name_others.append('temp')

        name_states = name_mom + name_conc + name_others
        name_params = [name for ind, name in
                       enumerate(self.KinInstance.name_params)
                       if self.mask_params[ind]]

        fig, axis = plot_sens(self.timeProf, sens_data,
                              name_states=name_states,
                              name_params=name_params,
                              mode=mode)

        return fig, axis

    def animate_cryst(self, filename=None, fps=5, step_data=1):
        from matplotlib.animation import FuncAnimation
        from matplotlib.animation import FFMpegWriter

        if type(self.timeProf) is list:
            self.flatten_states()

        if filename is None:
            filename = 'anim'

        fig_anim, ax_anim = plt.subplots(figsize=(5, 3.125))

        # ax_anim.set_xlim(0, self.x_grid.max())
        ax_anim.set_xlabel(r'crystal size ($\mu m$)')

        ax_anim.set_ylabel('counts')

        def func_data(ind):
            dist = self.distribProf[ind]
            return dist

        line, = ax_anim.plot(self.x_grid, func_data(0), '-o', mfc='None',
                             ms='2')
        time_tag = ax_anim.text(
            1, 1.04, '$time = {:.1f}$ s'.format(self.timeProf[0]),
            horizontalalignment='right',
            transform=ax_anim.transAxes)

        def func_anim(ind):
            f_vals = func_data(ind)
            line.set_ydata(f_vals)
            plt.gca().set_xscale("log")

            if f_vals.max() > f_vals.min():
                ax_anim.set_ylim(f_vals.min()*1.15, f_vals.max()*1.15)

            time_tag.set_text('$time = {:.1f}$ s'.format(self.timeProf[ind]))

            fig_anim.tight_layout()

        frames = np.arange(0, len(self.timeProf), step_data)
        animation = FuncAnimation(fig_anim, func_anim, frames=frames,
                                  repeat=True)

        writer = FFMpegWriter(fps=fps, metadata=dict(artist='Me'),
                              bitrate=1800)

        animation.save(filename + '.mp4', writer=writer)

        return animation, fig_anim, ax_anim


class BatchCryst(_BaseCryst):
    def __init__(self, target_ind, mask_params=None,
                 method='1D-FVM', scale=1,
                 isothermal=False, temp_control=None, adiabatic=False,
                 rad_zero=0, reset_states=False, name_species=None,
                 u_ht=1000, vol_ht=None, ht_media=None, basis='mass_conc',
                 jac_type=None):

        super().__init__(mask_params, method, target_ind,
                         scale, isothermal, temp_control, adiabatic,
                         rad_zero,
                         reset_states, name_species, u_ht, vol_ht, ht_media,
                         basis, jac_type)
        """ Construct a Batch Crystallizer object
        Parameters
        ----------
        """

        self.is_continuous = False
        self.states_uo.append('conc_target')
        self.states_uo.append('vol')

        self.oper_mode = 'Batch'

        self.nomenclature()

    def nomenclature(self):
        if not self.isothermal:
            if self.temp_control is None:
                self.states_uo.append('temp')

        self.names_states_out = ['mass_conc']

        if self.method == 'moments':
            self.names_states_in.insert(0, 'moments')
            self.names_states_out.insert(0, 'moments')

            self.states_uo.append('moments')

        elif self.method == '1D-FVM':
            self.names_states_in.insert(0, 'num_distrib')
            self.names_states_out.insert(0, 'total_distrib')

            self.states_uo.append('total_distrib')

        self.names_states_out = self.names_states_out + ['vol', 'temp']
        self.names_states_in += ['vol_liq', 'temp']

    def jac_states(self, time, states, params, return_only=True):

        if return_only:
            return self.jac_states_vals
        else:
            # Name states
            vol_liq = states[-1]

            num_material = self.num_distr + self.num_species
            w_conc = states[self.num_distr:num_material]

            temp = self.temp_control(time, self.temp,
                                    *self.params_control['temp'],
                                    t_zero=self.elapsed_time)

            num_states = len(states)
            conc_tg = w_conc[self.target_ind]
            c_sat = self.KinInstance.get_solubility(temp)

            # Kinetics
            b_pr = self.KinInstance.prim_nucl
            b_sec = self.KinInstance.sec_nucl

            rho_l = self.Liquid_1.getDensity(temp=temp)
            rho_c = self.Solid_1.getDensity(temp=temp)
            kv = self.Solid_1.kv

            nucl = b_pr + b_sec
            gr = self.KinInstance.growth

            g_exp = self.KinInstance.params['growth'][-1]
            bp_exp = self.KinInstance.params['nucl_prim'][-1]
            bs_exp = self.KinInstance.params['nucl_prim'][-2]
            bs2_exp = self.KinInstance.params['nucl_prim'][-1]

            moms = states[:self.num_distr]
            idx_moms = np.arange(1, len(moms))

            jacobian = np.zeros((num_states, num_states))

            # ----- Moments columns
            wrt_mu = idx_moms * gr

            rng = np.arange(len(wrt_mu))
            jacobian[rng + 1, rng] = wrt_mu

            # dfu0_du3
            jacobian[0, self.num_distr - 1] = vol_liq * bs2_exp * b_sec / moms[3]

            # Second moment column (concentration eqns)
            dtr_mu2 = 3 * kv * gr * rho_c * (1e-6)**3  # factor from material bce

            dfconc_dmu2 = -1/vol_liq * dtr_mu2 * (self.kron_jtg - w_conc/rho_l)
            jacobian[self.num_distr:self.num_distr + dfconc_dmu2.shape[0],
                    2] = dfconc_dmu2

            # Volume eqn
            jacobian[-1, 2] = -dtr_mu2 / rho_l  # dfvol/dmu2

            # ----- Concentration columns
            # Moment eqns
            db_dconc = (bp_exp * b_pr + bs_exp * b_sec) * vol_liq
            dg_dconc = idx_moms * g_exp * gr * moms[:-1]

            jacobian[0, self.num_distr +
                    self.target_ind] = db_dconc  # dfmu_0/dC_tg

            jacobian[
                1:1 + dg_dconc.shape[0],
                self.num_distr + self.target_ind] = dg_dconc  # dfmu_i/dC_tg

            jacobian[:self.num_distr, self.num_distr] *= 1 / (conc_tg - c_sat)

            # Concentration eqns
            tr = 3 * kv * gr * moms[2] * rho_c * (1e-6)**3
            dtr_dconc_tg = g_exp * tr / (conc_tg - c_sat)

            first_conc = np.outer(self.kron_jtg - w_conc/rho_l, self.kron_jtg)
            second_conc = tr/rho_l * np.eye(len(w_conc))

            dfconc_dconc = -1/vol_liq * (dtr_dconc_tg * first_conc + second_conc)

            jacobian[self.num_distr:-1, self.num_distr:-1] = dfconc_dconc

            # Volume eqn
            jacobian[-1, self.num_distr + self.target_ind] = - \
                dtr_dconc_tg / rho_l

            # ----- Volume column
            # mu_zero eqn
            jacobian[0, -1] = nucl  # dfmu_0/dvol

            # Concentration eqn
            dfconc_dvol = 1/vol_liq**2 * (self.kron_jtg*tr - w_conc/rho_l * tr)
            jacobian[self.num_distr:self.num_distr + dfconc_dvol.shape[0],
                    -1] = dfconc_dvol

            return jacobian

    def jac_params(self, time, states, params):

        temp = self.temp_control(time, self.temp,
                                 *self.params_control['temp'],
                                 t_zero=self.elapsed_time)

        num_states = len(states)

        vol_liq = states[-1]
        moms = states[:self.num_distr]
        num_material = self.num_distr + self.num_species
        w_conc = states[self.num_distr:num_material]
        conc_tg = w_conc[self.target_ind]

        kv = self.Solid_1.kv
        rho_c = self.Solid_1.getDensity(temp=temp)
        rho_l = self.Liquid_1.getDensity(temp=temp)

        b_sec = self.KinInstance.sec_nucl

        dbp, dbs, dg, _, _ = self.KinInstance.deriv_cryst(conc_tg, temp)
        dbs = np.append(dbs, b_sec*np.log(kv * moms[3]))

        # dg *= 1e-6  # to m/s

        num_bp = len(dbp)
        num_bs = len(dbs)
        num_nucl = len(dbp) + len(dbs)
        num_gr = len(dg)

        # TODO: the 3 is only to account for dissolution
        num_params = num_nucl + num_gr + 3

        idx_moms = np.arange(1, self.num_distr)
        g_section = np.outer(idx_moms * moms[:-1], dg)

        # ----- Moment equations
        jacobian = np.zeros((num_states, num_params))

        # Zeroth moment eqn
        jacobian[0, :num_bp] = dbp
        jacobian[0, num_bp:num_bp + num_bs] = vol_liq * dbs

        jacobian[0] *= vol_liq

        # 1 and higher order moments eqns
        jacobian[1:1 + g_section.shape[0],
                 num_nucl:num_nucl + g_section.shape[1]] = g_section

        # ----- Concentration eqns
        dtr_g = 3 * kv * rho_c * moms[2] * dg * (1e-6)**3  # factor from material bce
        dconc_dg = -1/vol_liq * np.outer(self.kron_jtg - w_conc/rho_l, dtr_g)

        jacobian[self.num_distr:self.num_distr + dconc_dg.shape[0],
                 num_nucl:num_nucl + dconc_dg.shape[1]] = dconc_dg

        # ----- Volume eqn
        jacobian[-1, num_nucl:num_nucl + dtr_g.shape[0]] = -dtr_g / rho_l

        return jacobian[:, self.mask_params]

    def get_inputs(self, time):
        return None

    def material_balances(self, time, distrib, w_conc, temp, vol_liq, params,
                          u_inputs, rhos, moms, phi_in=None):

        rho_liq, rho_s = rhos

        vol_solid = moms[1] * self.Solid_1.kv  # mu_3 is total, not by volume
        vol_slurry = vol_liq + vol_solid

        if self.method == 'moments':
            ddistr_dt, transf = self.method_of_moments(distrib, w_conc, temp,
                                                       params, rho_s,
                                                       vol_slurry)
        elif self.method == '1D-FVM':
            ddistr_dt, transf = self.fvm_method(distrib, moms, w_conc, temp,
                                                params, rho_s, vol_slurry)

        # Balance for target
        self.Liquid_1.updatePhase(mass_conc=w_conc, vol=vol_liq)

        dvol_liq = -transf/rho_liq
        dcomp_dt = -transf/vol_liq * (self.kron_jtg - w_conc/rho_liq)

        dliq_dt = np.append(dcomp_dt, dvol_liq)

        if self.basis == 'mass_frac':
            dcomp_dt *= 1 / rho_liq

        dmaterial_dt = np.concatenate((ddistr_dt, dliq_dt))

        return dmaterial_dt, transf

    def energy_balances(self, time, distr, conc, temp, temp_ht, vol_liq,
                        params, cryst_rate, u_inputs, rhos, moms,
                        h_in=None):

        vol_solid = moms[1] * self.Solid_1.kv  # mu_3 is total, not by volume
        vol_total = vol_liq + vol_solid

        phi = vol_liq / vol_total
        phis = [phi, 1 - phi]

        # Suspension properties
        capacitance = self.Slurry.getCp(temp, phis, rhos,
                                        times_vliq=True)

        # Renaming
        dh_cryst = -1.46e4  # J/kg
        # dh_cryst = -self.Liquid_1.delta_fus[self.target_ind] / \
        #     self.Liquid_1.mw[self.target_ind] * 1000  # J/kg

        vol = vol_liq / phi
        diam = (4/np.pi * vol)**(1/3)
        area_ht = np.pi * diam**2  # m**2

        source_term = dh_cryst*cryst_rate

        if self.adiabatic:
            ht_term = 0
        else:
            ht_term = self.u_ht*area_ht*(temp - temp_ht)

        # Balance inside the tank
        dtemp_dt = (-source_term - ht_term) / capacitance / vol_liq

        if temp_ht is not None:
            tht_in = self.temp_ht_in  # degC
            flow_ht = self.flow_ht
            cp_ht = 4180  # J/kg/K
            rho_ht = 1000
            vol_ht = vol*0.14  # m**3

            dtht_dt = flow_ht / vol_ht * (tht_in - temp_ht) - \
                self.u_ht*area_ht*(temp_ht - temp) / rho_ht/vol_ht/cp_ht

            return dtemp_dt, dtht_dt

        else:
            return dtemp_dt

    def retrieve_results(self, time, states):
        self.statesProf = states
        time_profile = np.array(time)

        # Decompose states
        self.timeProf.append(time_profile)
        distribProf = states[:, :self.num_distr] / self.scale
        self.distribProf.append(distribProf)
        self.volProf = states[:, self.num_distr + self.num_species]

        vol_liq = self.volProf[-1]

        if self.method == 'moments':
            mu_3_final = distribProf[-1][3]
        else:
            mu_3_final = self.Solid_1.getMoments(distrib=distribProf[-1],
                                                 mom_num=3)

        vol_sol = mu_3_final * self.Solid_1.kv

        rho_solid = self.Solid_1.getDensity()
        mass_sol = rho_solid * vol_sol

        vol_slurry = vol_liq + vol_sol
        num_material = self.num_distr + self.num_species
        y_outputs = np.delete(states, num_material, axis=1)

        if self.method == '1D-FVM':
            self.distribVolProf = distribProf * self.Solid_1.kv * self.x_grid**3

        if self.isothermal:
            self.tempProf.append(
                np.ones_like(time_profile) * self.Liquid_1.temp)

        elif self.temp_control is None:
            self.tempProf.append(states[:, -2])
            self.tempProfHt.append(states[:, -1])

            self.Liquid_1.temp = self.tempProf[-1][-1]
        else:
            temp_controlled = self.temp_control(
                time_profile, self.temp, *self.params_control['temp'],
                t_zero=self.elapsed_time)
            self.tempProf.append(temp_controlled)

            self.Liquid_1.temp = self.tempProf[-1][-1]
            # self.Liquid_1.tempProf = self.tempProf[-1]

        wConcProf = states[:, self.num_distr:self.num_distr + self.num_species]

        self.wConcProf.append(wConcProf)

        self.states = states[-1]
        self.temp = self.Liquid_1.temp

        if self.method == 'moments':
            self.Solid_1.moments = self.distribProf[-1][-1]
        else:
            self.Solid_1.distrib = self.distribProf[-1][-1]

        self.w_conc = self.wConcProf[-1][-1]

        self.Liquid_1.updatePhase(mass_conc=self.w_conc, vol=self.volProf[-1])

        if self.method == '1D-FVM':
            self.Solid_1.updatePhase(distrib=self.distribProf[-1][-1],
                                     mass=mass_sol)

        self.elapsed_time = time[-1]

        if self.name_species is None:
            conc_names = ['C_{}'.format(ind) for ind in
                          range(wConcProf.shape[1])]
        else:
            conc_names = ['C_{}'.format(name) for name in self.name_species]

        # self.name_states = self.name_states + conc_names

        # Create outlets
        liquid_out = copy.deepcopy(self.Liquid_1)
        solid_out = copy.deepcopy(self.Solid_1)

        if self.method == '1D-FVM':  # TODO: solid distrib shouldn't be per m3
            solid_out.updatePhase(distrib=distribProf[-1])

        self.Outlet = Slurry(vol_slurry=vol_slurry)
        self.Outlet.Phases = (liquid_out, solid_out)

        self.outputs = y_outputs


class MSMPR(_BaseCryst):
    def __init__(self, target_ind,
                 mask_params=None,
                 method='1D-FVM', scale=1,
                 isothermal=False, temp_control=None, adiabatic=False,
                 rad_zero=0, reset_states=False,
                 name_species=None,
                 u_ht=1000, vol_ht=None, ht_media=None, basis='mass_conc',
                 jac_type=None):

        super().__init__(KinInstance, Phases, mask_params, method, target_ind,
                         scale, isothermal, temp_control, adiabatic, rad_zero,
                         reset_states, name_species, u_ht, vol_ht, ht_media,
                         basis, jac_type)
        """ Construct a MSMPR object
        Parameters
        ----------
        oper_mode : str
            Operation mode of the reactor. It takes one of the following
            values: 'Batch', 'MSMPR', 'Semibatch'. If 'Semibatch', it is
            assumed that an antisolvent stream is entering the tank.
        """

        self.states_uo.append('conc_j')
        self.is_continuous = True
        self.oper_mode = 'Continuous'
        self._Inlet = None

        self.nomenclature()

    @property
    def Inlet(self):
        return self._Inlet

    @Inlet.setter
    def Inlet(self, inlet_object):
        self._Inlet = inlet_object

    def solve_steady_state(self, frac_seed, temp):

        vol = self.vol_slurry
        flow_v = self.Inlet.vol_flow / vol  # 1/s

        x_vec = self.Solid_1.x_distrib  # um

        kv = self.Solid_1.kv
        rho = self.Solid_1.getDensity(temp=temp)

        w_in = self.Inlet.Liquid_1.mass_frac[self.target_ind]

        def fun_of_frac(w_tank, full_output=False):

            nucl, growth, _ = self.KinInstance.get_kinetics(w_tank, temp, kv)

            # growth *= 1e-6

            # Analytical solution to f(x)
            f_zero = nucl / growth  # num / s / m**3 / um
            f_x = np.exp(-flow_v / growth * x_vec) * f_zero

            # vfrac_ph = self.Slurry.getFractions(f_x)
            # rho_liq = self.Liquid_1.getDensity(temp=temp)

            mu_2 = trapezoidal_rule(x_vec, x_vec**2 * f_x)

            kinetic_term = -3 * kv * rho * growth * mu_2
            flow_term = flow_v * (w_in - w_tank)

            conc_eqn = kinetic_term + flow_term

            if full_output:
                return f_x, conc_eqn
            else:
                return conc_eqn

        # Solve eqn
        # frac_seed = self.Liquid_1.mass_frac[self.target_ind]
        w_convg, info = newton(fun_of_frac, frac_seed, full_output=True)
        f_convg, final_fn = fun_of_frac(w_convg, full_output=True)

        return x_vec, f_convg, w_convg, info, final_fn

    def nomenclature(self):
        self.names_states_in += ['vol_flow', 'temp']

        if not self.isothermal:
            self.states_uo += ['temp', 'temp_ht']
        elif self.adiabatic:
            self.states_uo.append('temp')

        if self.method == 'moments':
            # mom_names = ['mu_%s0' % ind for ind in range(self.num_mom)]

            # for mom in mom_names[::-1]:
            self.names_states_in.insert(0, 'moments')

            self.states_uo.append('moments')

        elif self.method == '1D-FVM':
            self.names_states_in.insert(0, 'num_distrib')

            self.states_uo.append('num_distrib')

        self.names_states_out = self.names_states_in

    def get_inputs(self, time):
        if hasattr(self.Inlet, 'y_inlet'):
            all_inputs = self.Inlet.InterpolateInputs(time)
            input_upstream = get_dict_states(self.names_upstream,
                                             self.num_species, self.num_distr,
                                             all_inputs)

            input_dict = {}
            for key in self.names_states_in:
                input_dict[key] = input_upstream.get(self.bipartite[key])

            if input_dict['num_distrib'] is None:
                input_dict['num_distrib'] = np.zeros_like(
                    self.Solid_1.x_distrib)
        else:
            input_dict = {'temp': self.Inlet.Liquid_1.temp,
                          'mass_conc': self.Inlet.Liquid_1.mass_conc,
                          'vol_flow': self.Inlet.vol_flow,
                          'num_distrib': self.Inlet.Solid_1.distrib}

        return input_dict

    def material_balances(self, time, distrib, w_conc, temp, vol, params,
                          u_inputs, rhos, moms, phi_in):

        rho_sol = rhos[0][1]

        input_flow = u_inputs['vol_flow']
        input_distrib = u_inputs['num_distrib'] * self.scale
        input_conc = u_inputs['mass_conc']

        if self.method == 'moments':
            ddistr_dt, transf = self.method_of_moments(distrib, w_conc, temp,
                                                       params, rho_sol)

        elif self.method == '1D-FVM':
            ddistr_dt, transf = self.fvm_method(distrib, moms, w_conc, temp,
                                                params, rho_sol)

            self.Solid_1.moments[[2, 3]] = moms

        # ---------- Add flow terms
        # Distribution
        tau_inv = input_flow / vol
        flow_distrib = tau_inv * (input_distrib - distrib)

        ddistr_dt = ddistr_dt + flow_distrib

        # Liquid phase
        phi = 1 - self.Solid_1.kv * moms[1]

        c_tank = w_conc

        flow_term = tau_inv * (input_conc*phi_in[0] - c_tank*phi)
        transf_term = transf * (self.kron_jtg - c_tank / rho_sol)
        dcomp_dt = 1 / phi * (flow_term - transf_term)

        if self.basis == 'mass_frac':
            rho_liq = self.Liquid_1.getDensity()
            dcomp_dt *= 1 / rho_liq

        dmaterial_dt = np.concatenate((ddistr_dt, dcomp_dt))

        return dmaterial_dt, transf

    def energy_balances(self, time, distr, conc, temp, temp_ht, vol, params,
                        cryst_rate, u_inputs, rhos, moms, h_in):

        rho_susp, rho_in = rhos

        input_flow = u_inputs['vol_flow']
        input_temp = u_inputs['temp']

        # Thermodynamic properties (basis: slurry volume)
        phi_liq = 1 - self.Solid_1.kv * moms[1]

        phis = [phi_liq, 1 - phi_liq]
        h_sp = self.Slurry.getEnthalpy(temp, phis, rho_susp)
        capacitance = self.Slurry.getCp(temp, phis, rho_susp)  # J/m**3/K

        # Renaming
        dh_cryst = -1.46e4  # J/kg
        # dh_cryst = -self.Liquid_1.delta_fus[self.target_ind] / \
        #     self.Liquid_1.mw[self.target_ind] * 1000  # J/kg

        diam = (4/np.pi * vol)**(1/3)
        area_ht = np.pi * diam**2  # m**2

        # Energy terms (W)
        flow_term = input_flow * (h_in - h_sp)
        source_term = dh_cryst*cryst_rate * vol
        ht_term = self.u_ht*area_ht*(temp - temp_ht)

        # Balance inside the tank
        dtemp_dt = (flow_term - source_term - ht_term) / vol / capacitance

        # Balance in the jacket
        flow_ht = self.ht_media.vol_flow
        tht_in = self.ht_media.temp_in
        cp_ht = self.ht_media.cp
        rho_ht = self.ht_media.rho

        vol_ht = vol*0.14  # m**3

        dtht_dt = flow_ht / vol_ht * (tht_in - temp_ht) - \
            self.u_ht*area_ht*(temp_ht - temp) / rho_ht/vol_ht/cp_ht

        return dtemp_dt, dtht_dt

    def retrieve_results(self, time, states):
        self.statesProf = states

        time_profile = np.array(time)
        self.timeProf.append(time_profile)

        states[:, :self.num_distr] *= 1 / self.scale

        distProf = states[:, :self.num_distr]
        self.distribProf.append(distProf)

        if self.method == '1D-FVM':
            self.distribVolProf = distProf * self.Solid_1.kv * self.x_grid**3

        volflow = self.get_inputs(time_profile)['vol_flow']

        num_material = self.num_distr + self.num_species

        rho_solid = self.Solid_1.getDensity()
        if 'vol' in self.states_uo:
            self.volProf = states[:, num_material]
            y_outputs = np.delete(states, num_material, axis=1)
            vol_liq = self.volProf[-1]
            vol_sol = self.Solid_1.getMoments(distrib=distProf[-1],
                                              mom_num=3) * self.Solid_1.kv

            vol_div = vol_liq + vol_sol  # suspension volume
            mass_sol = rho_solid * vol_sol

        else:
            y_outputs = states
            vol_liq = self.Liquid_1.vol
            vol_div = 1

            mom_3 = self.Solid_1.getMoments(distrib=distProf[-1], mom_num=3)
            vol_sol = mom_3 * self.Solid_1.kv * self.vol_slurry

            mass_sol = rho_solid * vol_sol
            massflow_sol = mom_3 * self.Solid_1.kv * \
                self.get_inputs(time_profile[-1])['vol_flow'] * rho_solid

        if self.isothermal:
            temp_prof = np.ones_like(time_profile) * self.Liquid_1.temp
            self.tempProf.append(temp_prof)

            y_outputs = np.column_stack((y_outputs, volflow))
            y_outputs = np.column_stack((y_outputs, temp_prof))

        elif 'temp_ht' in self.states_uo:
            self.tempProf.append(states[:, -2])
            self.tempProfHt.append(states[:, -1])

            self.Liquid_1.temp = self.tempProf[-1][-1]

            y_outputs = y_outputs[:, :-1]
            y_outputs = np.insert(y_outputs, -1, volflow, axis=1)

        elif 'temp' in self.states_uo:
            self.tempProf.append(states[:, -1])
            self.Liquid_1.temp = self.tempProf[-1][-1]

            y_outputs = y_outputs[:, :-1]
            y_outputs = np.insert(y_outputs, -1, volflow, axis=1)

        else:
            temp_controlled = self.temp_control(
                time_profile, self.temp, *self.params_control['temp'],
                t_zero=self.elapsed_time)

            self.tempProf.append(temp_controlled)

            y_outputs = np.column_stack((y_outputs, volflow))
            y_outputs = np.column_stack((y_outputs, temp_controlled))

            self.Liquid_1.temp = self.tempProf[-1][-1]

        wConcProf = states[:, self.num_distr:num_material]

        self.wConcProf.append(wConcProf)

        self.states = states[-1]
        self.temp = self.Liquid_1.temp

        # Update phases
        self.Solid_1.updatePhase(distrib=self.distribProf[-1][-1],
                                 mass=mass_sol)
        self.Solid_1.temp = self.temp

        self.w_conc = self.wConcProf[-1][-1]

        self.Liquid_1.updatePhase(vol=vol_liq, mass_conc=self.w_conc)

        self.elapsed_time = time[-1]

        # Create output stream
        path = self.Liquid_1.path_data

        solid_comp = np.zeros(self.num_species)
        solid_comp[self.target_ind] = 1

        if type(self) == MSMPR:
            liquid_out = LiquidStream(path, mass_conc=self.w_conc,
                                      temp=self.temp)

            if self.method == '1D-FVM':
                solid_out = SolidStream(path,
                                        mass_frac=solid_comp,
                                        mass_flow=massflow_sol,
                                        x_distrib=self.x_grid,
                                        distrib=self.distribProf[-1][-1])
            else:
                solid_out = SolidStream(path, x_distrib=self.x_grid,
                                        moments=self.distribProf[-1][-1],
                                        mass_frac=solid_comp,
                                        mass_flow=massflow_sol)

            self.Outlet = SlurryStream(vol_flow=self.Inlet.vol_flow,
                                       x_distrib=self.x_grid,
                                       distrib=self.distribProf[-1][-1])

        else:
            # liquid_out = LiquidPhase(path, mass_conc=self.w_conc,
            #                          temp=self.temp)

            liquid_out = copy.deepcopy(self.Liquid_1)
            solid_out = copy.deepcopy(self.Solid_1)
            solid_out.updatePhase(distrib=self.distribProf[-1][-1] / vol_div)

            # if self.method == '1D-FVM':
            #     solid_out = SolidPhase(path, x_distrib=self.x_grid,
            #                            distrib=self.distribProf[-1][-1]/vol_div,
            #                            mass_frac=solid_comp)
            # else:
            #     # solid_out = SolidPhase(path, x_distrib=self.x_grid,
            #     #                        moments=self.distribProf[-1][-1],
            #     #                        mass_frac=solid_comp)

            self.Outlet = Slurry(vol_slurry=vol_div)

        self.outputs = y_outputs
        self.Outlet.Phases = (liquid_out, solid_out)


class SemibatchCryst(MSMPR):

    def nomenclature(self):
        self.states_uo.append('vol')

        self.names_states_out = ['mass_conc']

        if self.method == 'moments':
            mom_names = ['mu_%s0' % ind for ind in range(self.num_mom)]

            for mom in mom_names[::-1]:
                self.names_states_in.insert(0, mom)
                self.names_states_out.insert(0, 'total_%s' % mom)

            self.states_uo += mom_names

        elif self.method == '1D-FVM':
            self.names_states_in.insert(0, 'num_distrib')
            self.names_states_out.insert(0, 'total_distrib')

            self.states_uo.append('total_distrib')

        self.names_states_out = self.names_states_out + ['vol', 'temp']
        self.names_states_in += ['vol_flow', 'temp']

    def material_balances(self, time, distrib, w_conc, temp, vol_liq, params,
                          u_inputs, rhos, moms, phi_in):

        rho_susp, rho_in = rhos

        rho_liq, rho_sol = rho_susp
        rho_in_liq, _ = rho_in

        input_flow = u_inputs['vol_flow']
        input_distrib = u_inputs['num_distrib'] * self.scale
        input_conc = u_inputs['mass_conc']

        vol_solid = moms[1] * self.Solid_1.kv  # mu_3 is total, not by volume
        vol_slurry = vol_liq + vol_solid

        self.Liquid_1.updatePhase(mass_conc=w_conc)

        if self.method == 'moments':
            ddistr_dt, transf = self.method_of_moments(distrib, w_conc, temp,
                                                       params, rho_sol,
                                                       vol_slurry)

        elif self.method == '1D-FVM':
            ddistr_dt, transf = self.fvm_method(distrib, moms, w_conc, temp,
                                                params, rho_sol, vol_slurry)

        # ---------- Add flow terms
        # Distribution
        flow_distrib = input_flow * input_distrib

        ddistr_dt = ddistr_dt + flow_distrib

        # Liquid phase
        c_tank = w_conc

        flow_term = phi_in[0]*input_flow * (
            input_conc - w_conc * rho_in_liq/rho_liq)
        transf_term = transf * (self.kron_jtg - c_tank/rho_liq)

        dcomp_dt = 1/vol_liq * (flow_term - transf_term)
        dvol_dt = (phi_in[0] * input_flow * rho_in_liq - transf) / rho_liq

        dliq_dt = np.append(dcomp_dt, dvol_dt)

        if self.basis == 'mass_frac':
            dcomp_dt *= 1 / rho_liq

        dmaterial_dt = np.concatenate((ddistr_dt, dliq_dt))

        return dmaterial_dt, transf

    def energy_balances(self, time, distr, conc, temp, temp_ht, vol_liq,
                        params, cryst_rate, u_inputs, rhos, moms, h_in):

        rho_susp, rho_in = rhos

        # Input properties
        input_flow = u_inputs['vol_flow']

        vol_solid = moms[1] * self.Solid_1.kv  # mu_3 is total, not by volume
        vol_total = vol_liq + vol_solid

        phi = vol_liq / vol_total
        phis = [phi, 1 - phi]
        dens_slurry = np.dot(rho_susp, phis)

        # Suspension properties
        capacitance = self.Slurry.getCp(temp, phis, rho_susp,
                                        times_vliq=True)
        h_sp = self.Slurry.getEnthalpy(temp, phis, rho_susp)

        # Renaming
        dh_cryst = -1.46e4  # J/kg
        # dh_cryst = -self.Liquid_1.delta_fus[self.target_ind] / \
        #     self.Liquid_1.mw[self.target_ind] * 1000  # J/kg

        vol = vol_liq / phi
        diam = (4/np.pi * vol)**(1/3)
        area_ht = np.pi * diam**2  # m**2

        # Terms
        dens_in_liq = rho_in[0]
        dmass_dt = input_flow * dens_in_liq

        accum_term = dmass_dt * h_sp/dens_slurry
        flow_term = input_flow * h_in

        source_term = dh_cryst*cryst_rate

        if self.adiabatic:
            ht_term = 0
        else:
            ht_term = self.u_ht*area_ht*(temp - temp_ht)

        # Balance inside the tank
        dtemp_dt = (flow_term - source_term - ht_term - accum_term) / \
            capacitance / vol_liq

        # print(dtemp_dt)

        if temp_ht is not None:
            tht_in = self.temp_ht_in  # degC
            flow_ht = self.flow_ht
            cp_ht = 4180  # J/kg/K
            rho_ht = 1000
            vol_ht = vol*0.14  # m**3

            dtht_dt = flow_ht / vol_ht * (tht_in - temp_ht) - \
                self.u_ht*area_ht*(temp_ht - temp) / rho_ht/vol_ht/cp_ht

            return dtemp_dt, dtht_dt

        else:
            return dtemp_dt